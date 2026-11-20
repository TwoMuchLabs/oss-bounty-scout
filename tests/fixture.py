"""Builds a small git repository with a known regression for the tests."""

from __future__ import annotations

import subprocess
from pathlib import Path

CORE_V1 = '''"""Tiny statistics helpers."""


def mean(values):
    """Arithmetic mean; 0.0 for an empty sequence."""
    if not values:
        return 0.0
    total = sum(values)
    return total / len(values)


def spread(values):
    return max(values) - min(values)
'''

# The regression: the empty-input guard is dropped during a "simplification".
CORE_BROKEN = CORE_V1.replace("    if not values:\n        return 0.0\n", "")

ISSUE_BODY = """### Describe the bug

`mean([])` crashes after updating to main. It worked in 1.1.0.

### Steps to reproduce

1. Install statskit from main
2. Run the snippet below

```python
from statskit import mean

print(mean([]))
```

### Traceback

```
Traceback (most recent call last):
  File "/home/dev/repro.py", line 3, in <module>
    print(mean([]))
  File "/home/dev/.venv/lib/python3.12/site-packages/statskit/core.py", line 7, in mean
    return total / len(values)
ZeroDivisionError: division by zero
```

### Environment

- OS: Ubuntu 22.04
- Python: 3.12.1
- statskit: main
"""


def issue(number: int = 42, association: str = "MEMBER", body: str = ISSUE_BODY) -> dict:
    return {
        "number": number, "title": "mean([]) raises ZeroDivisionError", "body": body,
        "html_url": f"https://github.com/example/statskit/issues/{number}",
        "user": {"login": "reporter"}, "author_association": association,
        "created_at": "2026-11-20T06:00:00Z", "labels": [{"name": "bug"}],
    }


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.com",
         "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false", *args],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def _commit(repo: Path, message: str, files: dict[str, str]) -> str:
    for name, content in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def build(path: Path, commits_after: int = 6) -> dict:
    """Create the repo; returns {'repo', 'culprit', 'head'}."""
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    _commit(path, "Initial commit", {"statskit/__init__.py": "from .core import mean, spread\n",
                                     "statskit/core.py": CORE_V1, "README.md": "# statskit\n"})
    _commit(path, "Add changelog", {"CHANGELOG.md": "## 1.0.0\n"})
    _git(path, "tag", "v1.0.0")
    for i in range(3):
        _commit(path, f"Docs pass {i}", {"README.md": f"# statskit\n\nrevision {i}\n"})
    _git(path, "tag", "v1.1.0")
    for i in range(3):
        _commit(path, f"Add util {i}", {f"statskit/util{i}.py": f"VALUE = {i}\n"})
    culprit = _commit(path, "Simplify mean()", {"statskit/core.py": CORE_BROKEN, "CHANGELOG.md": "## 1.2.0\n- simplify mean\n\n## 1.0.0\n"})
    for i in range(commits_after):
        _commit(path, f"Later change {i}", {f"statskit/later{i}.py": f"X = {i}\n"})
    return {"repo": path, "culprit": culprit, "head": _git(path, "rev-parse", "HEAD")}
