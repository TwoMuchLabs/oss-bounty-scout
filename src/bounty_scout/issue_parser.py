"""Turn a raw GitHub issue into structured triage input.

The parser is deterministic and dependency-free. It never executes anything:
it only classifies what the reporter wrote into reproduction steps, runnable
code, error logs, stack frames, environment details and version hints.
"""

from __future__ import annotations

import ast
import re
from dataclasses import asdict, dataclass, field

# Heading text -> canonical section. Order matters: first match wins.
_SECTION_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("expected", re.compile(r"expect", re.I)),
    ("regression", re.compile(r"regression|worked before|last (known )?(good|working)", re.I)),
    ("repro", re.compile(r"reproduc|\brepro\b|\bsteps\b|how to|mcve|minimal|example|code", re.I)),
    ("env", re.compile(r"environment|version|system info|platform|\bsetup\b|configuration|\bos\b|operating system", re.I)),
    ("actual", re.compile(r"actual|what happened|current behaviou?r|observed|describe the bug|description|problem", re.I)),
    ("logs", re.compile(r"\blogs?\b|traceback|stack ?trace|error|output", re.I)),
]

_HEADING = re.compile(r"^\s{0,3}(?:#{1,6}\s+(?P<h>.+?)\s*#*|\*\*(?P<b>[^*]+?)\*\*:?)\s*$")
_FENCE = re.compile(r"^\s{0,3}(?P<fence>`{3,}|~{3,})\s*(?P<lang>[\w+#.-]*)")
_LIST_ITEM = re.compile(r"^\s*(?:\d+[.)]|[-*+])\s+(?P<item>.+)$")

_LOG_LANGS = {"pytb", "traceback", "log", "logs", "text", "txt", "output", "stderr", "stdout", "plaintext"}
_SHELL_LANGS = {"sh", "bash", "shell", "zsh", "console", "shell-session", "shellsession", "terminal"}
_PY_LANGS = {"python", "py", "python3", "py3"}
_JS_LANGS = {"js", "javascript", "node", "mjs", "cjs"}

# "ValueError: bad thing", "java.lang.IllegalStateException: ...", "TypeError: x is not a function"
_TYPED_ERROR = re.compile(
    r"^\s*(?:Caused by:\s*|Uncaught\s+|E\s+|(?:FAILED|ERROR) \S+ - )?(?P<type>[A-Za-z_][\w.]*(?:Error|Exception|Failure|Exit|Interrupt))(?::[ \t]*(?P<msg>.*))?\s*$",
    re.M,
)
_PANIC = re.compile(r"^\s*(?P<line>(?:panic:|thread '.*?' panicked at|fatal error:|Segmentation fault).*)$", re.M)
_GENERIC_ERROR = re.compile(r"^\s*(?P<line>(?:\w+: )?(?:error|ERROR|Error|FAILED|fatal)\b[: ].+)$", re.M)

# mypy / pyright style output: "pkg/mod.py:12: error: Argument 1 ..." or a bare "error: ... [arg-type]"
_TYPE_CHECK = re.compile(r"^\S+\.pyi?:\d+(?::\d+)?:\s*(?:error|note):|^\s*error:.*\[[a-z-]+\]\s*$|\b(?:mypy|pyright|pylance|reveal_type)\b", re.M | re.I)

_PY_FRAME = re.compile(r'File "(?P<file>[^"]+)", line (?P<line>\d+)')
_GENERIC_FRAME = re.compile(r"(?P<file>(?:[A-Za-z]:)?[\w./\\@+-]+\.[A-Za-z]{1,5}):(?P<line>\d+)\b")

_VERSION = r"v?\d+\.\d+(?:\.\d+)?(?:[-.+]?[A-Za-z0-9]+)*"
_REF = rf"(?P<ref>{_VERSION}|[0-9a-f]{{7,40}})"
_GOOD_HINT = re.compile(
    r"(?:work(?:s|ed|ing)?(?:\s+(?:fine|correctly|well|as expected))?\s+(?:in|on|with|until|up to)"
    r"|last\s+(?:known\s+)?(?:good|working)(?:\s+(?:version|release|commit))?(?:\s+(?:is|was))?:?"
    r"|downgrad\w+\s+to|revert\w*\s+to|roll\w*\s+back\s+to"
    r"|not\s+(?:present|reproducible|an issue)\s+(?:in|on|with))"
    r"\s+(?:version\s+|release\s+|tag\s+|commit\s+)?`?" + _REF,
    re.I,
)
_BAD_HINT = re.compile(
    r"(?:bro(?:ke|ken)\s+(?:in|since|with|after|by)"
    r"|since(?:\s+(?:upgrading|updating)\s+to)?"
    r"|after\s+(?:upgrading|updating|bumping)\s+to"
    r"|regress\w*\s+(?:in|since|from)"
    r"|start(?:s|ed|ing)\s+(?:with|from|in)"
    r"|fail(?:s|ed|ing)?\s+(?:in|on|with|since))"
    r"\s+(?:version\s+|release\s+|tag\s+|commit\s+)?`?" + _REF,
    re.I,
)

_ENV_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("os", re.compile(r"\b((?:Ubuntu|Debian|Fedora|CentOS|RHEL|Alpine|Arch Linux|macOS|Mac OS X|OS X|Windows)(?:[ \t]+(?:Server[ \t]+)?[\w.]+)?)", re.I)),
    ("python", re.compile(r"\bPython[ \t:=v]*(\d+\.\d+(?:\.\d+)?)", re.I)),
    ("node", re.compile(r"\bNode(?:\.js)?[ \t:=v]*(\d+\.\d+(?:\.\d+)?)", re.I)),
    ("go", re.compile(r"\bgo(?:lang)?[ \t]*(?:version[ \t]*)?(?:go)?(\d+\.\d+(?:\.\d+)?)\b")),
    ("rust", re.compile(r"\brustc[ \t]+(\d+\.\d+(?:\.\d+)?)")),
]
_KEY_VALUE = re.compile(r"^\s*(?:[-*+]\s+)?\**(?P<k>[A-Za-z][\w ./()-]{0,40}?)\**\s*[:=]\s*\**(?P<v>\S.*?)\**\s*$")
_TABLE_ROW = re.compile(r"^\s*\|\s*(?P<k>[^|]+?)\s*\|\s*(?P<v>[^|]+?)\s*\|\s*$")
_NO_RESPONSE = re.compile(r"^_?no response_?$", re.I)


@dataclass
class CodeBlock:
    lang: str
    text: str
    section: str  # canonical section the block appeared under ("" if none)
    kind: str  # "python" | "javascript" | "shell" | "log" | "unknown"


@dataclass
class ParsedIssue:
    number: int
    title: str
    url: str = ""
    author: str = ""
    author_association: str = "NONE"
    created_at: str = ""
    labels: list[str] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)
    code_blocks: list[CodeBlock] = field(default_factory=list)
    shell_commands: list[str] = field(default_factory=list)
    error_logs: list[str] = field(default_factory=list)
    error_signature: str | None = None
    stack_frames: list[tuple[str, int]] = field(default_factory=list)
    environment: dict[str, str] = field(default_factory=dict)
    good_hints: list[str] = field(default_factory=list)
    bad_hints: list[str] = field(default_factory=list)
    expected: str = ""
    actual: str = ""
    missing: list[str] = field(default_factory=list)
    pytest_output: bool = False  # the logs are pytest's, so the snippet is meant to be collected by pytest
    kind: str = "runtime"  # "type-check" when the report is about a type checker's output, not about running code

    def to_dict(self) -> dict:
        return asdict(self)


def canonical_section(heading: str) -> str:
    for name, pattern in _SECTION_RULES:
        if pattern.search(heading):
            return name
    return ""


def _looks_like_python(text: str) -> bool:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return False
    interesting = (ast.Call, ast.Import, ast.ImportFrom, ast.Assign, ast.FunctionDef, ast.ClassDef, ast.Assert)
    return any(isinstance(node, interesting) for node in ast.walk(tree))


def _looks_like_log(text: str) -> bool:
    return bool(
        "Traceback (most recent call last)" in text
        or _PANIC.search(text)
        or re.search(r"^\s+at .+\(.+:\d+", text, re.M)
        or (_TYPED_ERROR.search(text) and not _looks_like_python(text))
    )


def _split_console(text: str) -> tuple[list[str], str]:
    """Split a console transcript into ``$ ``-prefixed commands and the rest (output)."""
    commands, output = [], []
    for line in text.splitlines():
        m = re.match(r"^\s*(?:\$|%|>)\s+(.+)$", line)
        (commands if m else output).append(m.group(1) if m else line)
    return commands, "\n".join(output).strip()


def _split_repl(text: str) -> tuple[str, str]:
    """Split a ``>>>`` session into the code that was typed and what was printed."""
    code, output = [], []
    for line in text.splitlines():
        if line.startswith(">>> ") or line.startswith("... "):
            code.append(line[4:])
        elif line.rstrip() in (">>>", "..."):
            code.append("")
        else:
            output.append(line)
    return "\n".join(code).strip(), "\n".join(output).strip()


def _classify(lang: str, text: str, section: str) -> str:
    lang = lang.lower()
    if lang in _LOG_LANGS:
        return "log"
    if lang in _PY_LANGS:
        return "python"
    if lang in _JS_LANGS:
        return "javascript"
    if lang in _SHELL_LANGS:
        return "shell"
    if lang:
        return "unknown"
    if section == "logs" or _looks_like_log(text):
        return "log"
    if _looks_like_python(text):
        return "python"
    if re.search(r"^\s*\$\s+\S", text, re.M):
        return "shell"
    return "unknown"


_PYTEST_ASSERT = re.compile(r"^(?:FAILED|ERROR) \S+ - (assert .+)$", re.M)
_PYTEST_OUTPUT = re.compile(r"^(?:FAILED|ERROR) \S+::|^=+ (?:FAILURES|ERRORS|short test summary info) =+|^E   ", re.M)


def extract_signature(texts: list[str]) -> str | None:
    """Pick the most specific error line: a typed exception, then a panic, then a generic error."""
    for text in texts:
        if not _TYPED_ERROR.search(text):
            short = _PYTEST_ASSERT.search(text)  # pytest's summary drops the "AssertionError:" prefix
            if short:
                return f"AssertionError: {short.group(1).strip()}"
        typed = list(_TYPED_ERROR.finditer(text))
        if typed:
            m = typed[-1]  # last one is the root cause in Python and Java chains
            msg = (m.group("msg") or "").strip()
            return f"{m.group('type')}: {msg}" if msg else m.group("type")
    for pattern in (_PANIC, _GENERIC_ERROR):
        for text in texts:
            m = pattern.search(text)
            if m:
                return m.group("line").strip()
    return None


def extract_frames(text: str) -> list[tuple[str, int]]:
    """Return (file, line) pairs in the order they appear, de-duplicated."""
    found: list[tuple[int, str, int]] = []
    for pattern in (_PY_FRAME, _GENERIC_FRAME):
        for m in pattern.finditer(text):
            found.append((m.start(), m.group("file").replace("\\", "/"), int(m.group("line"))))
    seen: set[tuple[str, int]] = set()
    frames = []
    for _, file, line in sorted(found):
        if (file, line) not in seen and not file.startswith("<"):
            seen.add((file, line))
            frames.append((file, line))
    return frames


def _dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


def parse_issue(raw: dict) -> ParsedIssue:
    """Parse an issue as returned by the GitHub REST API (``gh api repos/O/R/issues/N``)."""
    body = (raw.get("body") or "").replace("\r\n", "\n")
    user = raw.get("user") or raw.get("author") or {}
    issue = ParsedIssue(
        number=int(raw.get("number") or 0),
        title=raw.get("title") or "",
        url=raw.get("html_url") or raw.get("url") or "",
        author=user.get("login", "") if isinstance(user, dict) else str(user),
        author_association=raw.get("author_association") or raw.get("authorAssociation") or "NONE",
        created_at=raw.get("created_at") or raw.get("createdAt") or "",
        labels=[l["name"] if isinstance(l, dict) else str(l) for l in raw.get("labels") or []],
    )

    section, heading = "", ""
    prose: dict[str, list[str]] = {}
    numbered_anywhere: list[str] = []
    fence, fence_lang, fence_lines = None, "", []

    for line in body.split("\n"):
        if fence is not None:
            closing = _FENCE.match(line)
            if closing and closing.group("fence")[0] == fence[0] and len(closing.group("fence")) >= len(fence) and not closing.group("lang"):
                _add_block(issue, fence_lang, "\n".join(fence_lines), section)
                fence = None
            else:
                fence_lines.append(line)
            continue
        opening = _FENCE.match(line)
        if opening:
            fence, fence_lang, fence_lines = opening.group("fence"), opening.group("lang"), []
            continue
        h = _HEADING.match(line)
        if h:
            heading = (h.group("h") or h.group("b")).strip()
            section = canonical_section(heading)
            prose.setdefault(section, [])
            continue
        if not line.strip():
            continue
        prose.setdefault(section, []).append(line)
        item = _LIST_ITEM.match(line)
        if item:
            if section == "repro":
                issue.steps.append(item.group("item").strip())
            elif re.match(r"^\s*\d+[.)]", line):
                numbered_anywhere.append(item.group("item").strip())
        if section == "env":
            _add_env_line(issue, heading, line)
    if fence is not None:  # unterminated fence: keep what we have
        _add_block(issue, fence_lang, "\n".join(fence_lines), section)

    if not issue.steps:
        issue.steps = numbered_anywhere
    for step in issue.steps:  # commands given inline, e.g. "run `pytest -k foo`"
        m = re.search(r"\b(?:run|execute|call|invoke)\b[^`]*`([^`]+)`", step, re.I)
        if m:
            issue.shell_commands.append(m.group(1))
    issue.shell_commands = _dedupe(issue.shell_commands)

    issue.expected = "\n".join(prose.get("expected", [])).strip()
    issue.actual = "\n".join(prose.get("actual", [])).strip()

    issue.error_logs = _dedupe([log for log in issue.error_logs if log])
    issue.error_signature = extract_signature(issue.error_logs) or extract_signature([body])
    frames_source = "\n".join(issue.error_logs) or body
    issue.stack_frames = extract_frames(frames_source)

    for key, pattern in _ENV_PATTERNS:
        if key not in issue.environment:
            m = pattern.search(body)
            if m:
                issue.environment[key] = m.group(1).strip()

    text = f"{issue.title}\n{body}"
    issue.good_hints = _dedupe([m.group("ref") for m in _GOOD_HINT.finditer(text)])
    issue.bad_hints = _dedupe([m.group("ref") for m in _BAD_HINT.finditer(text) if m.group("ref") not in issue.good_hints])

    issue.pytest_output = bool(_PYTEST_OUTPUT.search("\n".join(issue.error_logs) or body))
    signature = issue.error_signature or ""
    if not _TYPED_ERROR.match(signature) and _TYPE_CHECK.search("\n".join(issue.error_logs) or body) and (
            signature.lower().startswith("error:") or re.search(r"\.pyi?:\d+(?::\d+)?:\s*error:", body)):
        issue.kind = "type-check"

    runnable = [b for b in issue.code_blocks if b.kind in ("python", "javascript")] or issue.shell_commands
    if not runnable:
        issue.missing.append("runnable reproduction (a code snippet or shell commands)")
    if not issue.error_signature:
        issue.missing.append("error output or traceback")
    if not issue.environment:
        issue.missing.append("environment details (OS, runtime and package versions)")
    return issue


def _add_block(issue: ParsedIssue, lang: str, text: str, section: str) -> None:
    text = text.strip("\n")
    if not text.strip():
        return
    kind = _classify(lang, text, section)
    if kind in ("python", "unknown", "log") and re.search(r"^>>> ", text, re.M):
        code, output = _split_repl(text)
        if code:
            issue.code_blocks.append(CodeBlock(lang or "python", code, section, "python"))
            issue.error_logs.append(output)
            return
    if kind == "shell":
        commands, output = _split_console(text)
        if not commands:  # plain script: every non-comment line is a command
            commands = [l.strip() for l in text.splitlines() if l.strip() and not l.strip().startswith("#")]
            output = ""
        issue.shell_commands.extend(commands)
        if output and _looks_like_log(output):
            issue.error_logs.append(output)
    elif kind == "log":
        issue.error_logs.append(text)
    issue.code_blocks.append(CodeBlock(lang, text, section, kind))


def _add_env_line(issue: ParsedIssue, heading: str, line: str) -> None:
    if _NO_RESPONSE.match(line.strip()):
        return
    m = _TABLE_ROW.match(line) or _KEY_VALUE.match(line)
    if m:
        key, value = m.group("k").strip().lower(), m.group("v").strip().strip("`")
        if set(value) <= set("-: ") or key in ("key", "name", "field"):
            return  # table separator or header row
    elif heading and canonical_section(heading) == "env" and not _LIST_ITEM.match(line):
        # Issue forms render "### Python version\n\n3.12.1": the heading is the key.
        key, value = heading.lower(), line.strip().strip("`")
    else:
        return
    issue.environment.setdefault(key, value)
