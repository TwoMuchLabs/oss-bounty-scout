"""Thin wrappers around the GitHub CLI. Nothing here writes unless ``post_comment`` is called."""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timedelta, timezone

MARKER = "<!-- oss-bounty-scout -->"


class GhError(RuntimeError):
    pass


def _gh(*args: str, stdin: str | None = None) -> str:
    try:
        proc = subprocess.run(["gh", *args], capture_output=True, text=True, input=stdin, timeout=120)
    except FileNotFoundError as exc:
        raise GhError("the GitHub CLI (`gh`) is not installed; see https://cli.github.com") from exc
    if proc.returncode != 0:
        raise GhError(f"gh {' '.join(args[:3])} failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout


def parse_since(value: str, now: datetime | None = None) -> datetime:
    """Accept ``30m``, ``24h``, ``7d`` or an ISO-8601 timestamp."""
    now = now or datetime.now(timezone.utc)
    m = re.fullmatch(r"(\d+)([mhd])", value.strip())
    if m:
        unit = {"m": "minutes", "h": "hours", "d": "days"}[m.group(2)]
        return now - timedelta(**{unit: int(m.group(1))})
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def fetch_issue(repo: str, number: int) -> dict:
    return json.loads(_gh("api", f"repos/{repo}/issues/{number}"))


def fetch_new_issues(repo: str, since: datetime, limit: int = 30) -> list[dict]:
    """Open issues (not pull requests) created at or after ``since``, newest first."""
    stamp = since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = _gh("api", "--method", "GET", f"repos/{repo}/issues", "-f", "state=open", "-f", f"since={stamp}",
              "-f", "sort=created", "-f", "direction=desc", "-f", "per_page=100", "--paginate", "--slurp")
    pages = json.loads(out)
    issues = [i for page in pages for i in page]
    fresh = [
        i for i in issues
        if "pull_request" not in i and datetime.fromisoformat(i["created_at"].replace("Z", "+00:00")) >= since
    ]
    return fresh[:limit]


def post_comment(repo: str, number: int, body: str) -> str:
    """Post the report as an issue comment. Only called when ``--publish`` is passed."""
    return _gh("issue", "comment", str(number), "--repo", repo, "--body-file", "-", stdin=body).strip()
