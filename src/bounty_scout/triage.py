"""The triage pipeline: parse -> reproduce -> find baseline -> bisect -> localise -> suggest fix."""

from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import ai as ai_mod
from . import bisect as bisect_mod
from .config import Config
from .issue_parser import ParsedIssue, extract_frames, parse_issue
from .localize import Fix, Suspect, locate, resolve_frames, suggest_fix
from .repro import project_index
from .repro import (
    BAD, GOOD, CheckSpec, ReproPlan, Verdict, build_plan, complete_imports, evaluate, materialise, signature_pattern,
)
from .runner import Worktree, ensure_image, git


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
    ai: str | None = None  # off | anthropic | openai | command
    ai_model: str | None = None
    ai_base_url: str | None = None
    ai_cmd: str | None = None


@dataclass
class TriageResult:
    issue: ParsedIssue
    status: str  # reproduced | not-reproduced | inconclusive | needs-info | needs-approval | unsupported
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
    regression: str | None = None  # "yes" | "no" | "unknown", once history has been searched
    oldest_failing: str | None = None  # when regression == "no": the oldest version that already fails
    ai_label: str | None = None  # set when AI assistance was switched on
    ai_calls: int = 0
    ai_test_gate: str | None = None  # "reported-error" | "assertion" when the test in use was AI-written


def _tail(text: str, lines: int = 25) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])


def triage(repo_path: str | Path, raw_issue: dict, config: Config | None = None, options: Options | None = None) -> TriageResult:
    cfg, opts = config or Config(), options or Options()
    repo = Path(repo_path).resolve()
    issue = parse_issue(raw_issue)
    body = (raw_issue.get("body") or "").replace("\r\n", "\n")
    sandbox = opts.sandbox or cfg.sandbox
    result = TriageResult(issue, "needs-info", bad_ref=opts.bad, sandbox=sandbox)
    ai = ai_mod.from_settings(opts.ai or cfg.ai, opts.ai_model or cfg.ai_model, opts.ai_cmd or cfg.ai_command,
                               cfg.ai_attempts, opts.ai_base_url or cfg.ai_base_url)
    result.ai_label = ai.label if ai else None

    test_cmd = opts.test_cmd or (cfg.test if cfg.test and "{test_file}" not in cfg.test else None)
    if issue.kind == "type-check" and not test_cmd:
        result.status = "unsupported"
        result.notes.append(
            "This report is about what a type checker says, not about what the code does when it runs, so there is "
            "nothing here for the scout to execute. Pass `--test-cmd` with your type-check command to bisect it anyway."
        )
        return result
    plan = build_plan(issue, test_cmd, opts.match)
    if plan is None and not ai:
        result.notes.append("No runnable reproduction was found in the issue. Pass `--test-cmd` to supply one.")
        return result
    if plan and plan.test_filename and cfg.test and "{test_file}" in cfg.test:
        plan.command = cfg.test
    result.plan = plan
    if plan:
        result.notes += plan.notes

    trusted = (
        opts.trust_issue_code or sandbox == "docker"
        or issue.author_association.upper() in {a.upper() for a in cfg.trusted_associations}
    )
    if (plan is None or plan.uses_issue_code) and not trusted:
        result.status = "needs-approval"
        what = ("The reproduction comes from the issue body" if plan else
                "The report has no runnable code, and an AI-written test would be built from the issue text,")
        result.notes.append(
            f"{what} and its author is `{issue.author_association}`, so nothing was "
            "executed. Review it, then re-run with `--trust-issue-code` or `--sandbox docker`."
        )
        return result

    result.bad_sha = bisect_mod.resolve(repo, opts.bad)
    try:
        with tempfile.TemporaryDirectory(prefix="scout-repro-") as tmp:
            repro_dir = Path(tmp)
            repro_dir.chmod(0o755)  # readable by the unprivileged container user
            _run(repo, result, issue, body, plan, cfg, opts, sandbox, repro_dir, ai)
    finally:
        result.ai_calls = ai.calls if ai else 0
    return result


_DEFAULT_IMAGE = Config().docker_image


def _sandbox_image(repo, result, issue, plan: ReproPlan, cfg) -> str:
    """Pick the container image: Node for JavaScript, the reporter's Python where the image name allows, plus packages."""
    image = cfg.docker_image
    if plan.source == "node-snippet" and image == _DEFAULT_IMAGE:
        return cfg.node_image
    if "{python}" in image:
        stated = re.match(r"3\.(\d+)", issue.environment.get("python", ""))
        if stated:
            minor, why = int(stated.group(1)), "the version in the report"
        else:
            year = git(repo, "show", "-s", "--format=%cd", "--date=format:%Y", result.bad_sha).stdout.strip()
            # Roughly the newest Python at the time of the commit: 3.8 in 2020, 3.9 in 2021, and so on.
            minor, why = (int(year) - 2012 if year.isdigit() else 0), "the era of the commit under test"
        known = sorted(int(v.split(".")[1]) for v in cfg.docker_python_versions)
        default = int(cfg.docker_python.split(".")[1])
        pick = min(known, key=lambda k: abs(k - minor)) if (stated or minor < default) else default
        if pick != default:
            note = f"Ran on Python 3.{pick}, to match {why}."
            if note not in result.notes:
                result.notes.append(note)
        image = image.replace("{python}", f"3.{pick}")
    return ensure_image(image, cfg.docker_packages)


def _attempt(repo, result, plan: ReproPlan, cfg, opts, sandbox, repro_dir, issue=None):
    """Write the test, run it once at the failing ref, and return (spec, verdict, frames, command)."""
    if plan.source == "python-snippet":
        with Worktree(repo, result.bad_sha) as wt:  # read-only look at the project to fill in forgotten imports
            before = len(plan.notes)
            complete_imports(plan, wt.path)
            result.notes += plan.notes[before:]
    command = materialise(plan, repro_dir, sandbox)
    spec = CheckSpec(
        command=command, setup=opts.setup_cmd or cfg.setup,
        signature_pattern=signature_pattern(plan.signature), signature=plan.signature,
        timeout=opts.timeout or cfg.timeout, sandbox=sandbox, repro_dir=str(repro_dir),
        docker_image=_sandbox_image(repo, result, issue or result.issue, plan, cfg) if sandbox == "docker" else cfg.docker_image,
        docker_network=cfg.docker_network, pass_env=cfg.pass_env,
    )
    with Worktree(repo, result.bad_sha) as wt:
        head = evaluate(spec, wt.path)
        prefix = "/work" if sandbox == "docker" else str(wt.path)
        frames = resolve_frames(repo, result.bad_sha, extract_frames(head.result.output), prefix)
    return spec, head, frames, command


def _ai_test(repo, result, issue, body, original, cfg, opts, sandbox, repro_dir, ai):
    """Ask the model for a test. It is used only if it fails with the reported error at the failing ref."""
    with Worktree(repo, result.bad_sha) as wt:
        packages, _ = project_index(wt.path)
        tree, excerpts = ai_mod.project_context(wt.path, resolve_frames(repo, result.bad_sha, issue.stack_frames), packages)
    feedback = ""
    if original:
        feedback = f"The reporter's own snippet {original[1].reason}. Its output ended:\n{_tail(original[1].result.output, 12)}"
    gate = "reported-error" if issue.error_signature else "assertion"
    tried = 0
    for _ in range(ai.attempts):
        try:
            code = ai_mod.propose_test(ai, issue, body, tree, excerpts, packages, feedback)
        except ai_mod.AIError as exc:
            result.notes.append(f"AI assistance failed: {exc}.")
            break
        tried += 1
        if code is None:
            feedback = "The reply was not valid Python."
            continue
        header = f"# Test for issue #{issue.number}, written by an AI model ({ai.label}) from the report's description.\n"
        plan = ReproPlan("ai-test", "python3 {test_file}", f"test_issue_{issue.number}.py", header + code,
                         issue.error_signature or "AssertionError", uses_issue_code=True)
        attempt = _attempt(repo, result, plan, cfg, opts, sandbox, repro_dir, issue)
        if attempt[1].status == BAD:
            result.ai_test_gate = gate
            return plan, attempt
        feedback = f"It {attempt[1].reason}. Its output ended:\n{_tail(attempt[1].result.output, 12)}"
    if tried:
        wanted = "failed with the reported error" if issue.error_signature else "failed on an assertion"
        result.notes.append(f"The AI model proposed {tried} test(s); none {wanted}, so none was used.")
    return None


def _ai_fix(repo, result, issue, body, spec, ai) -> Fix | None:
    """Ask the model for a patch. It is used only if the failing test then passes."""
    suspect, culprit = result.suspect, result.bisect.culprit.sha if result.bisect and result.bisect.culprit else None
    culprit_diff = git(repo, "show", "--format=%s", "--no-color", culprit).stdout if culprit else ""
    feedback, tried = "", 0
    for _ in range(ai.attempts):
        with Worktree(repo, result.bad_sha) as wt:
            if not (wt.path / suspect.file).is_file():
                return None
            source, window = ai_mod.source_window(wt.path, suspect.file, suspect.line)
            try:
                edits = ai_mod.propose_edits(ai, issue, body, result.plan.test_content or result.command,
                                             result.head.result.output, suspect.file, source, window, culprit_diff, feedback)
            except ai_mod.AIError as exc:
                result.notes.append(f"AI assistance failed: {exc}.")
                break
            tried += 1
            error = ai_mod.apply_edits(wt.path, edits)
            if error:
                feedback = error
                continue
            diff = git(wt.path, "diff", "--no-color", "--no-ext-diff", "HEAD").stdout
            verdict = evaluate(spec, wt.path, clean=False)
            if diff.strip() and verdict.status == GOOD:
                return Fix(diff, "ai", "verified")
            feedback = f"With your change the test still does not pass: it {verdict.reason}. Output ended:\n{_tail(verdict.result.output, 12)}"
    if tried:
        result.notes.append(f"The AI model proposed {tried} patch(es); none made the failing test pass, so none was used.")
    return None


def _run(repo, result, issue, body, plan, cfg, opts, sandbox, repro_dir, ai) -> None:
    original = _attempt(repo, result, plan, cfg, opts, sandbox, repro_dir, issue) if plan else None
    attempt = original
    # The model is asked only when the report gave us nothing that reproduces: no code, or code that fails differently.
    if ai and (original is None or (original[1].status not in (GOOD, BAD) and plan.source == "python-snippet")):
        accepted = _ai_test(repo, result, issue, body, original, cfg, opts, sandbox, repro_dir, ai)
        if accepted:
            plan, attempt = accepted
            result.plan = plan
    if attempt is None:
        result.notes.append("No runnable reproduction was found in the issue. Pass `--test-cmd` to supply one.")
        return
    if plan.test_filename:  # earlier attempts share the file name; make sure the one in use is on disk
        (repro_dir / plan.test_filename).write_text(plan.test_content or "")
    spec, head, head_frames, result.command = attempt
    result.head = head

    if head.status == GOOD:
        result.status = "not-reproduced"
        return
    if head.status != BAD:
        result.status = "inconclusive"
        result.notes.append(f"The reproduction did not behave as reported at `{opts.bad}`: {head.reason}.")
        module = re.search(r"ModuleNotFoundError: No module named '([\w.]+)'", head.result.output)
        if module:
            result.notes.append(
                f"It needs the module `{module.group(1)}`, which is not available where the test ran. "
                "Install it with `--setup-cmd`, or use a `docker_image` that includes it."
            )
        return
    result.status = "reproduced"

    if opts.bisect:
        _trace(repo, result, issue, cfg, opts, spec, head_frames)
    if result.ai_test_gate == "assertion" and not result.good_ref:
        # Nothing ties this test to the report: no error to match, and no version where it passes.
        result.status = "inconclusive"
        result.notes.append(
            "The AI-written test fails, but the report gives no error to check it against and no earlier version "
            "passes it, so it does not show that the reported bug exists. Read the test before relying on it."
        )
        return
    if not result.suspect and head_frames:
        file, line = head_frames[-1]
        result.suspect = Suspect(file, line, "low", "the innermost project frame in the failure; no culprit commit was identified")
    if ai and opts.bisect and result.suspect and not (result.fix and result.fix.status == "verified"):
        result.fix = _ai_fix(repo, result, issue, body, spec, ai) or result.fix


def _trace(repo, result, issue, cfg, opts, spec, head_frames) -> None:
    """Find a good baseline, bisect, localise, and try the revert."""
    if bisect_mod.is_shallow(repo):
        result.notes.append("This is a shallow clone, so history is not available to bisect. Fetch full history (`fetch-depth: 0`).")
        return
    explicit = opts.good or cfg.good
    candidates = [explicit] if explicit else bisect_mod.candidate_good_refs(repo, result.bad_sha, issue.good_hints)
    good, probes = bisect_mod.find_good(repo, candidates, spec)
    result.probes = [(ref, v.status, v.reason) for ref, v in probes]
    if good is None:
        failing = [ref for ref, verdict in probes if verdict.status == BAD]
        if failing:
            # Candidates run newest to oldest, so the last failing one is the oldest version that has the bug.
            oldest = failing[-1][:10] if re.fullmatch(r"[0-9a-f]{40}", failing[-1]) else failing[-1]
            result.regression, result.oldest_failing = "no", oldest
            result.notes.append(
                f"Probably not a regression: it already fails the same way at `{oldest}`, the oldest version that "
                "could be tested. Pass `--good <ref>` if you know a version where it worked."
            )
        else:
            result.regression = "unknown"
            result.notes.append(
                "No earlier version could be tested here (tried: " + (", ".join(f"`{c}`" for c in candidates) or "none")
                + "); they fail for other reasons, such as needing an older runtime. Pass `--good <ref>` if you know a working version."
            )
        return
    result.good_ref, result.regression = good, "yes"

    result.bisect = bisect_mod.bisect(repo, good, result.bad_sha, spec, opts.first_parent)
    if result.bisect.status != "found":
        return
    culprit = result.bisect.culprit.sha
    frames = resolve_frames(repo, result.bad_sha, issue.stack_frames) + head_frames
    result.suspect = locate(repo, culprit, result.bad_sha, frames)
    result.fix = suggest_fix(repo, culprit, result.bad_sha, result.suspect, spec)
