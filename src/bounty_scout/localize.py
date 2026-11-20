"""From a culprit commit to a suspected file/line and a candidate fix."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .repro import GOOD, CheckSpec, evaluate
from .runner import Worktree, git

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
_NOT_SOURCE = re.compile(
    r"(^|/)(tests?|testing|docs?|examples?|\.github)/|(^|/)(test_[^/]+|[^/]+_test\.[^/]+|conftest\.py)$"
    r"|\.(md|rst|txt|lock|toml|cfg|ini|ya?ml|json)$|(^|/)(CHANGELOG|CHANGES|NEWS|LICENSE)[^/]*$",
    re.I,
)


@dataclass
class Hunk:
    file: str
    start: int  # first changed line in the culprit commit's version of the file
    count: int  # added/modified lines (0 for a pure deletion)


@dataclass
class Suspect:
    file: str
    line: int | None
    confidence: str  # "high" | "medium" | "low"
    reason: str

    @property
    def location(self) -> str:
        return f"{self.file}:{self.line}" if self.line else self.file


@dataclass
class Fix:
    diff: str
    scope: str  # "file" (revert of the culprit's change to one file) | "commit" (full revert) | "ai" (model-written)
    status: str  # "verified" | "still-fails" | "does-not-apply" | "unverified"


def changed_hunks(repo: str | Path, sha: str) -> list[Hunk]:
    out = git(repo, "show", "--format=", "--unified=0", "--no-color", "--no-ext-diff", "-M", "--first-parent", sha).stdout
    hunks: list[Hunk] = []
    current: str | None = None
    for line in out.splitlines():
        if line.startswith("+++ "):
            current = None if line[4:] == "/dev/null" else line[4:].removeprefix("b/")
        elif current and (m := _HUNK.match(line)):
            hunks.append(Hunk(current, int(m.group(1)), int(m.group(2) or 1)))
    return hunks


def resolve_frames(repo: str | Path, ref: str, frames: list[tuple[str, int]], strip_prefix: str | None = None) -> list[tuple[str, int]]:
    """Map paths from a traceback (often absolute, or from site-packages) onto tracked files."""
    tracked = git(repo, "ls-tree", "-r", "--name-only", ref).stdout.splitlines()
    by_name: dict[str, list[str]] = {}
    for path in tracked:
        by_name.setdefault(path.rsplit("/", 1)[-1], []).append(path)
    resolved = []
    for file, line in frames:
        if strip_prefix and file.startswith(strip_prefix):
            file = file[len(strip_prefix):].lstrip("/")
        parts = [p for p in file.split("/") if p not in ("", ".")]
        if not parts:
            continue
        best, best_len = None, 0
        for candidate in by_name.get(parts[-1], []):
            cparts = candidate.split("/")
            n = 0
            while n < min(len(parts), len(cparts)) and parts[-1 - n] == cparts[-1 - n]:
                n += 1
            # Longest shared suffix wins; a bare filename only counts when unambiguous.
            if n > best_len or (n == best_len and best and len(candidate) < len(best)):
                best, best_len = candidate, n
        if best and (best_len > 1 or len(by_name[parts[-1]]) == 1):
            resolved.append((best, line))
    return resolved


def _blamed_commit(repo: str | Path, ref: str, file: str, line: int) -> str | None:
    proc = git(repo, "blame", "--porcelain", "-L", f"{line},{line}", ref, "--", file, check=False)
    return proc.stdout.split(" ", 1)[0] if proc.returncode == 0 and proc.stdout else None


def locate(repo: str | Path, culprit: str, bad: str, frames: list[tuple[str, int]]) -> Suspect | None:
    """Rank where in the culprit commit the regression most likely lives.

    ``frames`` must be relative to ``bad`` (innermost frame last).
    """
    hunks = changed_hunks(repo, culprit)
    if not hunks:
        return None
    touched = {h.file for h in hunks}
    in_culprit = [(f, l) for f, l in frames if f in touched]

    for file, line in reversed(in_culprit):
        if _blamed_commit(repo, bad, file, line) == culprit:
            return Suspect(file, line, "high", "a stack frame of the failure lands on a line last changed by the culprit commit")
    # Deleted lines leave nothing to blame. If the file is unchanged since the culprit,
    # line numbers still line up, so a frame right next to a hunk is as good as a blame hit.
    for file, line in reversed(in_culprit):
        if git(repo, "diff", "--quiet", culprit, bad, "--", file, check=False).returncode == 0:
            if any(h.file == file and h.start - 3 <= line <= h.start + h.count + 3 for h in hunks):
                return Suspect(file, line, "high", "a stack frame of the failure is adjacent to lines the culprit commit changed or removed")
    if in_culprit:
        file, line = in_culprit[-1]
        return Suspect(file, line, "medium", "the failure's stack passes through a file changed by the culprit commit")

    source = [h for h in hunks if not _NOT_SOURCE.search(h.file)] or hunks
    per_file: dict[str, int] = {}
    for h in source:
        per_file[h.file] = per_file.get(h.file, 0) + max(h.count, 1)
    file = max(per_file, key=lambda f: per_file[f])
    biggest = max((h for h in source if h.file == file), key=lambda h: h.count)
    only = len(per_file) == 1
    return Suspect(
        file, biggest.start, "medium" if only else "low",
        "the only source file changed by the culprit commit" if only
        else "the most heavily changed source file in the culprit commit (no stack frame matched)",
    )


def _revert_diff(repo: str | Path, culprit: str, file: str | None) -> str:
    args = ["diff", "--no-color", "--no-ext-diff", culprit, f"{culprit}^"]
    return git(repo, *(args + (["--", file] if file else []))).stdout


def suggest_fix(repo: str | Path, culprit: str, bad: str, suspect: Suspect | None, spec: CheckSpec | None) -> Fix | None:
    """Propose the smallest revert that makes the reproduction pass at ``bad``.

    This is a candidate revert, not a designed fix: it restores the previous
    behaviour of the suspect file (or, failing that, of the whole commit).
    """
    attempts = ([("file", suspect.file)] if suspect else []) + [("commit", None)]
    first: Fix | None = None
    for scope, file in attempts:
        diff = _revert_diff(repo, culprit, file)
        if not diff.strip():
            continue
        if not spec:
            return Fix(diff, scope, "unverified")
        status, applied = _verify(repo, bad, diff, spec)
        fix = Fix(applied or diff, scope, status)
        if status == "verified":
            return fix
        first = first or fix
    return first


def _verify(repo: str | Path, bad: str, diff: str, spec: CheckSpec) -> tuple[str, str | None]:
    """Apply the revert to ``bad`` and re-run the reproduction.

    Returns the status and the patch as it applies to ``bad``. Later commits
    often touched the same file, so a plain apply is followed by a three-way
    merge; only a clean merge counts.
    """
    with Worktree(repo, bad) as wt:
        patch = wt.path.parent / "fix.patch"
        patch.write_text(diff)
        if git(wt.path, "apply", "--check", str(patch), check=False).returncode == 0:
            git(wt.path, "apply", str(patch))
        elif git(wt.path, "apply", "--3way", str(patch), check=False).returncode != 0:
            return "does-not-apply", None
        applied = git(wt.path, "diff", "--no-color", "--no-ext-diff", "HEAD").stdout
        if not applied.strip():
            return "does-not-apply", None
        status = "verified" if evaluate(spec, wt.path, clean=False).status == GOOD else "still-fails"
        return status, applied
