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
}
_FIX_STATUS = {
    "verified": "Verified: with this patch applied to `{bad}`, the reproduction passes.",
    "still-fails": "Not verified: the patch applies to `{bad}` but the reproduction still fails.",
    "does-not-apply": "Not verified: the patch no longer applies cleanly to `{bad}`; later commits touched the same lines.",
    "unverified": "Not verified.",
}
_EXT_LANG = {"py": "python", "js": "javascript", "sh": "sh"}


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


def render(r: TriageResult) -> str:
    issue, bad = r.issue, (r.bad_sha[:10] or r.bad_ref)
    out = [MARKER, f"## Triage report: #{issue.number} {_safe(issue.title)}", ""]

    rows = [("Reproduction", _reproduction_cell(r, bad))]
    b = r.bisect
    if b and b.status == "found":
        c = b.culprit
        rows.append(("Introduced by", f"`{c.short}` {_safe(c.subject)} ({_safe(c.author)}, {c.date})"))
        rows.append(("Bisect range", f"`{b.good}` (good) → `{bad}` (bad): {b.commits_in_range} commits, {len(b.steps)} tested"))
    elif b and b.status == "ambiguous":
        rows.append(("Introduced by", "One of: " + ", ".join(f"`{c.short}`" for c in b.candidates)))
    elif r.status == "reproduced":
        rows.append(("Introduced by", "Not determined"))
    if r.suspect:
        rows.append(("Suspected root cause", f"`{r.suspect.location}` ({r.suspect.confidence} confidence: {r.suspect.reason})"))
    if issue.error_signature:
        rows.append(("Reported error", f"`{_safe(issue.error_signature)[:200]}`"))
    out += ["| | |", "|---|---|"] + [f"| **{k}** | {v} |" for k, v in rows] + [""]

    if r.notes or (b and b.detail):
        out += ["### Notes", ""] + [f"- {n}" for n in r.notes]
        if b and b.detail:
            out.append(f"- {b.detail}" if b.status in ("ambiguous", "abandoned") else f"- Bisect did not finish: {_safe(b.detail)[:500]}")
        out.append("")

    if r.fix and r.fix.scope == "ai":
        out += [
            "### Suggested fix", "",
            f"**Written by an AI model** (`{r.ai_label}`). It was kept because the failing test passes with it applied to `{bad}`. "
            "Passing one test does not make a patch correct: nobody has reviewed it.",
            "", _fence(r.fix.diff, "diff"), "",
        ]
    elif r.fix:
        scope = f"reverts the culprit's change to `{r.suspect.file}`" if r.fix.scope == "file" and r.suspect else "reverts the culprit commit"
        out += [
            "### Suggested fix", "",
            f"Candidate patch ({scope}). It restores the previous behaviour; it is a starting point, not a reviewed fix.",
            "", _FIX_STATUS[r.fix.status].format(bad=bad), "", _fence(r.fix.diff, "diff"), "",
        ]

    if r.plan and r.plan.test_content:
        ext = (r.plan.test_filename or "").rsplit(".", 1)[-1]
        shown = r.plan.command.replace("{test_file}", r.plan.test_filename)
        origin = []
        if r.plan.source == "ai-test":
            why = ("It was kept because it fails with the error the reporter described." if r.ai_test_gate == "reported-error"
                   else "The report gave no error to check it against; it fails on an assertion the model wrote. Read it before relying on it.")
            origin = [f"**Written by an AI model** (`{r.ai_label}`) from the report's description, because nothing in the report reproduced the bug as written. {why}", ""]
        out += ["### Failing test", ""] + origin + [f"`{r.plan.test_filename}`, run as `{shown}`:", "",
                _fence(r.plan.test_content, _EXT_LANG.get(ext, "")), ""]
    elif r.plan:
        out += ["### Failing test", "", f"Command: `{r.plan.command}`", ""]

    if r.head and r.head.result.output.strip():
        out += [_details(f"Output at <code>{bad}</code> (exit {r.head.result.exit_code}, {r.head.result.duration:.1f}s)",
                         _fence(_tail(r.head.result.output))), ""]

    out += ["### Extracted from the issue", ""]
    if issue.environment:
        out += ["| Environment | |", "|---|---|"] + [f"| {_safe(k)} | {_safe(v)} |" for k, v in issue.environment.items()] + [""]
    if issue.steps:
        out += ["Steps to reproduce:", ""] + [f"{i}. {_safe(s)}" for i, s in enumerate(issue.steps, 1)] + [""]
    hints = [f"reported working in `{h}`" for h in issue.good_hints] + [f"reported broken in `{h}`" for h in issue.bad_hints]
    if hints:
        out += ["Version hints: " + "; ".join(hints) + ".", ""]
    if issue.missing:
        out += ["Missing from the report:", ""] + [f"- {m}" for m in issue.missing] + [""]

    if r.probes or (b and b.steps):
        lines = [f"- baseline probe `{ref}`: {verdict} ({reason})" for ref, verdict, reason in r.probes]
        lines += [f"- `{s['sha'][:10]}`: {s['verdict']} ({s['reason']}, {s['seconds']}s)" for s in (b.steps if b else [])]
        out += [_details("Bisect log", "\n".join(lines)), ""]

    isolation = "Docker container with networking disabled" if r.sandbox == "docker" else "temporary git worktree"
    ai = f"AI assistance: on ({r.ai_label}, {r.ai_calls} call{'s' if r.ai_calls != 1 else ''})" if r.ai_label else "AI assistance: off"
    out.append(f"<sub>Generated by oss-bounty-scout {__version__} in a {isolation}. {ai}. Automated analysis: verify before acting on it.</sub>")
    return "\n".join(out) + "\n"


def _reproduction_cell(r: TriageResult, bad: str) -> str:
    label = _STATUS[r.status]
    if r.status == "reproduced":
        return f"**{label}** at `{bad}` ({r.head.reason})"
    if r.status == "not-reproduced":
        return f"**{label}** at `{bad}`: the test ran and passed ({r.head.reason})"
    if r.status == "inconclusive":
        return f"**{label}** at `{bad}`: {r.head.reason}"
    return f"**{label}**"
