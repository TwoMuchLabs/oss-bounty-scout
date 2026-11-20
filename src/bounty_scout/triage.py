"""The triage pipeline: parse -> reproduce -> find baseline -> bisect -> localise -> suggest fix."""

from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import bisect as bisect_mod
from .config import Config
from .issue_parser import ParsedIssue, extract_frames, parse_issue
from .localize import Fix, Suspect, locate, resolve_frames, suggest_fix
from .repro import (
    BAD, GOOD, CheckSpec, ReproPlan, Verdict, build_plan, complete_imports, evaluate, materialise, signature_pattern,
)
from .runner import Worktree


@dataclass
class Options:
    good: str | None = None
    bad: str = "HEAD"
    test_cmd: str | None = None
    setup_cmd: str | None = None
    match: str | None = None
    timeout: float | None = None
    sandbox: str | None = None
    trust_issue_code: bool = False
    first_parent: bool = False
    bisect: bool = True


@dataclass
class TriageResult:
    issue: ParsedIssue
    status: str  # reproduced | not-reproduced | inconclusive | needs-info | needs-approval
    bad_ref: str = "HEAD"
    bad_sha: str = ""
    plan: ReproPlan | None = None
    command: str = ""
    sandbox: str = "worktree"
    head: Verdict | None = None
    good_ref: str | None = None
    probes: list[tuple[str, str, str]] = field(default_factory=list)  # (ref, verdict, reason)
    bisect: bisect_mod.BisectResult | None = None
    suspect: Suspect | None = None
    fix: Fix | None = None
    notes: list[str] = field(default_factory=list)


def triage(repo_path: str | Path, raw_issue: dict, config: Config | None = None, options: Options | None = None) -> TriageResult:
    cfg, opts = config or Config(), options or Options()
    repo = Path(repo_path).resolve()
    issue = parse_issue(raw_issue)
    sandbox = opts.sandbox or cfg.sandbox
    result = TriageResult(issue, "needs-info", bad_ref=opts.bad, sandbox=sandbox)

    test_cmd = opts.test_cmd or (cfg.test if cfg.test and "{test_file}" not in cfg.test else None)
    plan = build_plan(issue, test_cmd, opts.match)
    if plan is None:
        result.notes.append("No runnable reproduction was found in the issue. Pass `--test-cmd` to supply one.")
        return result
    if plan.test_filename and cfg.test and "{test_file}" in cfg.test:
        plan.command = cfg.test
    result.plan = plan
    result.notes += plan.notes

    trusted = (
        opts.trust_issue_code or sandbox == "docker"
        or issue.author_association.upper() in {a.upper() for a in cfg.trusted_associations}
    )
    if plan.uses_issue_code and not trusted:
        result.status = "needs-approval"
        result.notes.append(
            f"The reproduction comes from the issue body and its author is `{issue.author_association}`, so it was "
            "not executed. Review the generated test, then re-run with `--trust-issue-code` or `--sandbox docker`."
        )
        return result

    result.bad_sha = bisect_mod.resolve(repo, opts.bad)
    with tempfile.TemporaryDirectory(prefix="scout-repro-") as tmp:
        repro_dir = Path(tmp)
        repro_dir.chmod(0o755)  # readable by the unprivileged container user
        with Worktree(repo, result.bad_sha) as wt:  # read-only look at the project to fill in forgotten imports
            before = len(plan.notes)
            complete_imports(plan, wt.path)
            result.notes += plan.notes[before:]
        result.command = materialise(plan, repro_dir, sandbox)
        spec = CheckSpec(
            command=result.command, setup=opts.setup_cmd or cfg.setup,
            signature_pattern=signature_pattern(plan.signature), signature=plan.signature,
            timeout=opts.timeout or cfg.timeout, sandbox=sandbox, repro_dir=str(repro_dir),
            docker_image=cfg.docker_image, docker_network=cfg.docker_network, pass_env=cfg.pass_env,
        )

        with Worktree(repo, result.bad_sha) as wt:
            head = evaluate(spec, wt.path)
            prefix = "/work" if sandbox == "docker" else str(wt.path)
            head_frames = resolve_frames(repo, result.bad_sha, extract_frames(head.result.output), prefix)
        result.head = head
        if head.status == GOOD:
            result.status = "not-reproduced"
            return result
        if head.status != BAD:
            result.status = "inconclusive"
            result.notes.append(f"The reproduction did not behave as reported at `{opts.bad}`: {head.reason}.")
            module = re.search(r"ModuleNotFoundError: No module named '([\w.]+)'", head.result.output)
            if module:
                result.notes.append(
                    f"It needs the module `{module.group(1)}`, which is not available where the test ran. "
                    "Install it with `--setup-cmd`, or use a `docker_image` that includes it."
                )
            return result
        result.status = "reproduced"
        if not opts.bisect:
            return result

        if bisect_mod.is_shallow(repo):
            result.notes.append("This is a shallow clone, so history is not available to bisect. Fetch full history (`fetch-depth: 0`).")
            return result
        explicit = opts.good or cfg.good
        candidates = [explicit] if explicit else bisect_mod.candidate_good_refs(repo, result.bad_sha, issue.good_hints)
        good, probes = bisect_mod.find_good(repo, candidates, spec)
        result.probes = [(ref, v.status, v.reason) for ref, v in probes]
        if good is None:
            result.notes.append(
                "No known-good commit was found (tried: " + (", ".join(f"`{c}`" for c in candidates) or "none")
                + "), so this may not be a regression. Pass `--good <ref>` if you know a working version."
            )
            return result
        result.good_ref = good

        result.bisect = bisect_mod.bisect(repo, good, result.bad_sha, spec, opts.first_parent)
        if result.bisect.status != "found":
            return result
        culprit = result.bisect.culprit.sha
        frames = resolve_frames(repo, result.bad_sha, issue.stack_frames) + head_frames
        result.suspect = locate(repo, culprit, result.bad_sha, frames)
        result.fix = suggest_fix(repo, culprit, result.bad_sha, result.suspect, spec)
    return result
