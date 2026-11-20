"""Automated ``git bisect`` in a throw-away worktree."""

from __future__ import annotations

import json
import re
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .repro import BAD, EXIT_CODES, GOOD, CheckSpec, Verdict, evaluate
from .runner import GitError, Worktree, git

# git < 2.4x prints "is the first bad commit"; newer releases quote the term: "is the first 'bad' commit".
_FIRST_BAD = re.compile(r"^([0-9a-f]{40,64}) is the first '?bad'? commit", re.M)
_SHA_LINE = re.compile(r"^([0-9a-f]{40,64})$", re.M)

# ``git bisect run`` invokes this with the worktree as cwd. ``-I`` keeps the
# repository under test from shadowing our modules.
_STUB = """import sys
sys.path.insert(0, {package_parent!r})
from bounty_scout.bisect import predicate_main
sys.exit(predicate_main(sys.argv[1]))
"""


@dataclass
class Commit:
    sha: str
    subject: str = ""
    author: str = ""
    date: str = ""

    @property
    def short(self) -> str:
        return self.sha[:10]


@dataclass
class BisectResult:
    status: str  # "found" | "ambiguous" | "failed"
    good: str
    bad: str
    culprit: Commit | None = None
    candidates: list[Commit] = field(default_factory=list)  # when skips make it ambiguous
    steps: list[dict] = field(default_factory=list)  # one entry per tested commit
    commits_in_range: int = 0
    log: str = ""
    detail: str = ""


def is_shallow(repo: str | Path) -> bool:
    return git(repo, "rev-parse", "--is-shallow-repository").stdout.strip() == "true"


def resolve(repo: str | Path, ref: str) -> str:
    return git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").stdout.strip()


def describe(repo: str | Path, sha: str) -> Commit:
    out = git(repo, "show", "-s", "--format=%H%x00%s%x00%an%x00%ad", "--date=short", sha).stdout.strip()
    full, subject, author, date = (out.split("\x00") + ["", "", ""])[:4]
    return Commit(full, subject, author, date)


def _is_ancestor(repo: str | Path, older: str, newer: str) -> bool:
    return git(repo, "merge-base", "--is-ancestor", older, newer, check=False).returncode == 0


def candidate_good_refs(repo: str | Path, bad: str, hints: list[str], max_tags: int = 9) -> list[str]:
    """Where to look for a known-good commit: reporter hints, recent tags, then the root."""
    bad_sha = resolve(repo, bad)
    candidates: list[str] = []
    for hint in hints:
        for ref in (hint, f"v{hint}", hint.lstrip("v")):
            try:
                sha = resolve(repo, ref)
            except GitError:
                continue
            if sha != bad_sha and _is_ancestor(repo, sha, bad_sha):
                candidates.append(ref)
                break
    tags = git(repo, "tag", "--merged", bad_sha, "--sort=-creatordate").stdout.split()
    tags = [t for t in tags if resolve(repo, t) != bad_sha]
    # Newest first, then at widening intervals: recent regressions are found fast, old ones are still reachable.
    picks, step = [0, 1], 2
    while step < len(tags) and len(picks) < max_tags:
        picks.append(step)
        step *= 2
    candidates += [tags[i] for i in picks if i < len(tags)]
    candidates += git(repo, "rev-list", "--max-parents=0", bad_sha).stdout.split()
    seen: set[str] = set()
    unique = []
    for ref in candidates:
        sha = resolve(repo, ref)
        if sha not in seen and sha != bad_sha:
            seen.add(sha)
            unique.append(ref)
    return unique


def find_good(repo: str | Path, candidates: list[str], spec: CheckSpec) -> tuple[str | None, list[tuple[str, Verdict]]]:
    """Return the first candidate where the reproduction passes, plus every probe made."""
    probes: list[tuple[str, Verdict]] = []
    for ref in candidates:
        with Worktree(repo, ref) as wt:
            verdict = evaluate(spec, wt.path)
        probes.append((ref, verdict))
        if verdict.status == GOOD:
            return ref, probes
    return None, probes


def predicate_main(spec_path: str) -> int:
    """Entry point for ``git bisect run``: judge the commit checked out in cwd."""
    spec = CheckSpec.load(spec_path)
    verdict = evaluate(spec, Path.cwd())
    if spec.log_path:
        sha = git(Path.cwd(), "rev-parse", "HEAD").stdout.strip()
        with open(spec.log_path, "a") as fh:
            fh.write(json.dumps({"sha": sha, "verdict": verdict.status, "reason": verdict.reason,
                                 "seconds": round(verdict.result.duration, 2)}) + "\n")
    return EXIT_CODES[verdict.status]


def bisect(repo: str | Path, good: str, bad: str, spec: CheckSpec, first_parent: bool = False) -> BisectResult:
    """Run ``git bisect`` between a verified-good and verified-bad ref.

    Bisect state lives in a temporary worktree, so the maintainer's checkout,
    index and any bisect they have in progress are untouched.
    """
    good_sha, bad_sha = resolve(repo, good), resolve(repo, bad)
    count = int(git(repo, "rev-list", "--count", f"{good_sha}..{bad_sha}").stdout.strip() or 0)
    result = BisectResult("failed", good, bad, commits_in_range=count)
    if not _is_ancestor(repo, good_sha, bad_sha):
        result.detail = f"{good} is not an ancestor of {bad}; cannot bisect between them."
        return result

    with tempfile.TemporaryDirectory(prefix="scout-bisect-") as tmp:
        tmp_path = Path(tmp)
        spec.log_path = str(tmp_path / "steps.jsonl")
        spec_file = tmp_path / "spec.json"
        spec.dump(spec_file)
        stub = tmp_path / "predicate.py"
        stub.write_text(_STUB.format(package_parent=str(Path(__file__).resolve().parent.parent)))

        with Worktree(repo, bad_sha) as wt:
            try:
                start = ["bisect", "start"] + (["--first-parent"] if first_parent else [])
                git(wt.path, *start, bad_sha, good_sha)
                run = git(wt.path, "bisect", "run", sys.executable, "-I", str(stub), str(spec_file),
                          check=False, timeout=None)
                output = run.stdout + run.stderr
                result.log = git(wt.path, "bisect", "log", check=False).stdout
            finally:
                git(wt.path, "bisect", "reset", check=False)

        log_file = Path(spec.log_path)
        if log_file.exists():
            result.steps = [json.loads(line) for line in log_file.read_text().splitlines() if line]
        spec.log_path = None

    found = _FIRST_BAD.search(output)
    if found:
        result.status, result.culprit = "found", describe(repo, found.group(1))
    elif "only 'skip'ped commits left" in output:
        tail = output.split("could be any of:", 1)[-1]
        result.status = "ambiguous"
        result.candidates = [describe(repo, sha) for sha in _SHA_LINE.findall(tail)]
        result.detail = "Some commits could not be tested, so the first bad commit is one of several."
    else:
        result.detail = output.strip()[-2000:] or "git bisect run produced no result"
    return result
