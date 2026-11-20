#!/usr/bin/env python3
"""Save real GitHub issues, so the parser and the field test can be checked offline.

    python scripts/record_github.py corpus OUT.json.gz OWNER/NAME [OWNER/NAME ...] [--pages N]
    python scripts/record_github.py fetch  OUT.json.gz

`corpus` keeps closed and open issues (not pull requests) of each project, reduced to
the fields the tool reads. `fetch` runs the field test's own `fetch` command against a
few projects and keeps every answer `gh api` gave, keyed by request path, so the command
can be replayed without a token. Both need GH_TOKEN and the GitHub CLI.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "fieldtest")]

KEPT = ("number", "title", "body", "html_url", "author_association", "created_at", "closed_at", "state", "state_reason")
# (project, snippet kind, reports with an error but no code?) for the `fetch` recording
FETCHES = [("pallets/werkzeug", "python", False), ("expressjs/express", "node", False), ("pallets/click", "python", True)]


def reduced(issue: dict) -> dict:
    out = {key: issue.get(key) for key in KEPT}
    out["user"] = {"login": (issue.get("user") or {}).get("login", "")}
    out["labels"] = [{"name": label.get("name", "")} for label in issue.get("labels") or []]
    return out


def record_corpus(out: str, repos: list[str], pages: int) -> int:
    import field_test

    corpus = {}
    for repo in repos:
        issues = []
        for page in range(1, pages + 1):
            batch = field_test.gh(f"repos/{repo}/issues?state=all&sort=created&direction=desc&per_page=100&page={page}") or []
            issues += [reduced(i) for i in batch if "pull_request" not in i]
            if not batch:
                break
        corpus[repo] = issues
        print(f"{repo}: {len(issues)} issues", file=sys.stderr)
    with gzip.open(out, "wt", encoding="utf-8") as fh:
        json.dump(corpus, fh, sort_keys=True)
    return 0 if all(corpus.values()) else 1


def record_fetch(out: str) -> int:
    import field_test

    tape: dict = {}
    real = field_test.gh

    def gh_and_keep(path: str):
        tape[path] = real(path)
        return tape[path]

    field_test.gh = gh_and_keep
    with tempfile.TemporaryDirectory() as tmp:
        for repo, kind, prose in FETCHES:
            args = argparse.Namespace(repo=repo, output=os.path.join(tmp, "issues.json"), pages=4, issues=120, kind=kind, prose=prose)
            field_test.cmd_fetch(args)
    with gzip.open(out, "wt", encoding="utf-8") as fh:
        json.dump(tape, fh, sort_keys=True)
    print(f"{len(tape)} answers kept in {out}", file=sys.stderr)
    return 0 if tape else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("corpus")
    p.add_argument("out"); p.add_argument("repos", nargs="+"); p.add_argument("--pages", type=int, default=5)
    p = sub.add_parser("fetch")
    p.add_argument("out")
    args = parser.parse_args()
    return record_corpus(args.out, args.repos, args.pages) if args.cmd == "corpus" else record_fetch(args.out)


if __name__ == "__main__":
    sys.exit(main())
