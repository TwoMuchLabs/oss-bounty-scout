"""Render a triage result as Markdown."""

from __future__ import annotations

from . import __version__
from .github import MARKER
from .triage import TriageResult

_STATUS = {
    "reproduced": "Reproduced",
    "not-reproduced": "Not reproduced",
    "inconclusive": "Inconclusive",
    "needs-info": "Needs more information",
    "needs-approval": "Waiting for maintainer approval",
    "unsupported": "Not something this tool can run",
}
_FIX_STATUS = {
    "verified": "Verified: with this patch applied to `{bad}`, the reproduction passes.",
    "still-fails": "Not verified: the patch applies to `{bad}` but the reproduction still fails.",
    "does-not-apply": "Not verified: the patch no longer applies cleanly to `{bad}`; later commits touched the same lines.",
    "unverified": "Not verified.",
}
_EXT_LANG = {"py": "python", "js": "javascript", "cjs": "javascript", "mjs": "javascript", "sh": "sh"}


def _fence(text: str, lang: str = "") -> str:
    ticks = "```"
    while ticks in text:
        ticks += "`"
    return f"{ticks}{lang}\n{text.rstrip()}\n{ticks}"


def _safe(text: str) -> str:
    """Keep reporter-supplied prose from pinging people or breaking table cells."""
    return text.replace("@", "@\u200b").replace("|", "\\|").replace("\n", " ").strip()


def _details(summary: str, body: str) -> str:
    return f"<details>\n<summary>{summary}</summary>\n\n{body}\n\n</details>"


def _tail(text: str, lines: int = 40) -> str:
    rows = text.rstrip().splitlines()
    return "\n".join(rows[-lines:]) if len(rows) > lines else "\n".join(rows)


def _origin_rows(r: TriageResult, bad: str) -> list[tuple[str, str]]:
    """Where the bug came from, as far as the history search could tell."""
    b = r.bisect
    if b and b.status == "found":
        c = b.culprit
        return [("Introduced by", f"`{c.short}` {_safe(c.subject)} ({_safe(c.author)}, {c.date})"),
                ("Bisect range", f"`{b.good}` (good) → `{bad}` (bad): {b.commits_in_range} commits, {len(b.steps)} tested")]
    if b and b.status == "ambiguous":
        return [("Introduced by", "One of: " + ", ".join(f"`{c.short}`" for c in b.candidates))]
    if r.regression == "no":
        return [("Regression", f"Probably not: already fails at `{r.oldest_failing}`, the oldest version that could be tested")]
    if r.status == "reproduced":
        return [("Introduced by", "Not determined")]
    return []


def _summary_table(r: TriageResult, bad: str) -> list[str]:
    rows = [("Reproduction", _reproduction_cell(r, bad))]
    level = confidence(r)
    if level:
        rows.append(("Confidence", f"**{level[0].capitalize()}**: {level[1]}"))
    rows += _origin_rows(r, bad)
    if r.suspect:
        rows.append(("Suspected root cause", f"`{r.suspect.location}` ({r.suspect.confidence} confidence: {r.suspect.reason})"))
    if r.issue.error_signature:
        rows.append(("Reported error", f"`{_safe(r.issue.error_signature)[:200]}`"))
    return ["| | |", "|---|---|"] + [f"| **{k}** | {v} |" for k, v in rows] + [""]


def _notes_section(r: TriageResult) -> list[str]:
    b = r.bisect
    stopped = b.detail if b else ""
    if not (r.notes or stopped):
        return []
    out = ["### Notes", ""] + [f"- {n}" for n in r.notes]
    if stopped:
        out.append(f"- {stopped}" if b.status in ("ambiguous", "abandoned") else f"- Bisect did not finish: {_safe(stopped)[:500]}")
    return out + [""]


def _fix_section(r: TriageResult, bad: str) -> list[str]:
    if not r.fix:
        return []
    if r.fix.scope == "ai":
        return [
            "### Suggested fix", "",
            f"**Written by an AI model** (`{r.ai_label}`). It was kept because the failing test passes with it applied to `{bad}`. "
            "Passing one test does not make a patch correct: nobody has reviewed it.",
            "", _fence(r.fix.diff, "diff"), "",
        ]
    scope = {"hunk": f"undoes one part of the culprit's change to `{r.suspect.file}`" if r.suspect else "",
             "file": f"reverts the culprit's change to `{r.suspect.file}`" if r.suspect else ""}.get(r.fix.scope) or "reverts the culprit commit"
    return [
        "### Suggested fix", "",
        f"Candidate patch ({scope}). It restores the previous behaviour; it is a starting point, not a reviewed fix.",
        "", _FIX_STATUS[r.fix.status].format(bad=bad), "", _fence(r.fix.diff, "diff"), "",
    ]


def _test_section(r: TriageResult) -> list[str]:
    plan = r.plan
    if not plan:
        return []
    if not plan.test_content:
        return ["### Failing test", "", f"Command: `{plan.command}`", ""]
    ext = (plan.test_filename or "").rsplit(".", 1)[-1]
    shown = plan.command.replace("{test_file}", plan.test_filename)
    origin = []
    if plan.source == "ai-test":
        why = ("It was kept because it fails with the error the reporter described." if r.ai_test_gate == "reported-error"
               else "The report gave no error to check it against; it fails on an assertion the model wrote. Read it before relying on it.")
        origin = [f"**Written by an AI model** (`{r.ai_label}`) from the report's description, because nothing in the report reproduced the bug as written. {why}", ""]
    return ["### Failing test", ""] + origin + [f"`{plan.test_filename}`, run as `{shown}`:", "",
            _fence(plan.test_content, _EXT_LANG.get(ext, "")), ""]


def _output_section(r: TriageResult, bad: str) -> list[str]:
    if not (r.head and r.head.result.output.strip()):
        return []
    return [_details(f"Output at <code>{bad}</code> (exit {r.head.result.exit_code}, {r.head.result.duration:.1f}s)",
                     _fence(_tail(r.head.result.output))), ""]


def _extracted_section(issue) -> list[str]:
    out = ["### Extracted from the issue", ""]
    if issue.environment:
        out += ["| Environment | |", "|---|---|"] + [f"| {_safe(k)} | {_safe(v)} |" for k, v in issue.environment.items()] + [""]
    if issue.steps:
        out += ["Steps to reproduce:", ""] + [f"{i}. {_safe(s)}" for i, s in enumerate(issue.steps, 1)] + [""]
    hints = [f"reported working in `{h}`" for h in issue.good_hints] + [f"reported broken in `{h}`" for h in issue.bad_hints]
    if hints:
        out += ["Version hints: " + "; ".join(hints) + ".", ""]
    if issue.missing:
        out += ["Missing from the report:", ""] + [f"- {m}" for m in issue.missing] + [""]
    return out


def _bisect_log(r: TriageResult) -> list[str]:
    steps = r.bisect.steps if r.bisect else []
    if not (r.probes or steps):
        return []
    lines = [f"- baseline probe `{ref}`: {verdict} ({reason})" for ref, verdict, reason in r.probes]
    lines += [f"- `{s['sha'][:10]}`: {s['verdict']} ({s['reason']}, {s['seconds']}s)" for s in steps]
    return [_details("Bisect log", "\n".join(lines)), ""]


def _footer(r: TriageResult) -> str:
    isolation = "Docker container with networking disabled" if r.sandbox == "docker" else "temporary git worktree"
    ai = f"AI assistance: on ({r.ai_label}, {r.ai_calls} call{'s' if r.ai_calls != 1 else ''})" if r.ai_label else "AI assistance: off"
    return f"<sub>Generated by oss-bounty-scout {__version__} in a {isolation}. {ai}. Automated analysis: verify before acting on it.</sub>"


def render(r: TriageResult) -> str:
    bad = r.bad_sha[:10] or r.bad_ref
    out = [MARKER, f"## Triage report: #{r.issue.number} {_safe(r.issue.title)}", ""]
    out += _summary_table(r, bad) + _notes_section(r) + _fix_section(r, bad) + _test_section(r) + _output_section(r, bad)
    out += _extracted_section(r.issue) + _bisect_log(r)
    out.append(_footer(r))
    return "\n".join(out) + "\n"


def confidence(r: TriageResult) -> tuple[str, str] | None:
    """How far to trust a "reproduced", and why.

    The levels follow what replaying real issues showed: when an earlier version
    passes the test, the maintainers' own fix almost always makes it pass again;
    when every version fails, the "bug" is quite often behaviour that was kept.
    """
    if r.status != "reproduced":
        return None
    doubts = []
    if not r.in_project:
        doubts.append("the error never passes through the project's own files")
    if r.match == "similar":
        doubts.append("the error is similar to the reported one, not identical")
    elif r.match == "type-only":
        doubts.append("only the error type could be compared")
    elif r.match == "exit-code":
        doubts.append("the command failed, but there was no reported error to compare with")
    if r.ai_test_gate:
        doubts.append("the test was written by an AI model")
    if r.regression == "yes":
        if not doubts:
            return "high", f"an earlier version (`{r.good_ref}`) passes this test and the reported error appears exactly"
        return "medium", f"an earlier version (`{r.good_ref}`) passes this test, but " + "; ".join(doubts)
    if r.regression == "no":
        doubts.insert(0, "every version that could be tested fails the same way, so this may be how it has always behaved")
        return ("low" if len(doubts) > 1 else "medium"), "; ".join(doubts)
    doubts.insert(0, "no earlier version was tested")
    return ("low" if len(doubts) > 1 else "medium"), "; ".join(doubts)


def _reproduction_cell(r: TriageResult, bad: str) -> str:
    label = _STATUS[r.status]
    if r.status == "reproduced":
        return f"**{label}** at `{bad}` ({r.head.reason})"
    if r.status == "not-reproduced":
        return f"**{label}** at `{bad}`: the test ran and passed ({r.head.reason})"
    if r.status == "inconclusive":
        return f"**{label}** at `{bad}`: {r.head.reason}"
    return f"**{label}**"
