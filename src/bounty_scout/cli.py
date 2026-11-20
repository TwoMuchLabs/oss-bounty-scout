"""Command line interface for oss-bounty-scout."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from . import __version__, config as config_mod, github
from .issue_parser import parse_issue
from .report import render
from .runner import GitError, git
from .triage import Options, triage


def _detect_repo(repo_path: Path) -> str | None:
    url = git(repo_path, "remote", "get-url", "origin", check=False).stdout.strip()
    m = re.search(r"github\.com[:/]+([^/]+/[^/]+?)(?:\.git)?/?$", url)
    return m.group(1) if m else None


def _load_issue_file(path: str) -> list[dict]:
    data = json.loads(sys.stdin.read() if path == "-" else Path(path).read_text())
    return data if isinstance(data, list) else [data]


def _collect_issues(args) -> list[dict]:
    if args.issue_file:
        return _load_issue_file(args.issue_file)
    repo = _require_repo(args)
    if args.issue:
        return [github.fetch_issue(repo, n) for n in args.issue]
    return github.fetch_new_issues(repo, github.parse_since(args.since), args.limit)


def _require_repo(args) -> str:
    repo = args.repo or _detect_repo(Path(getattr(args, "repo_path", ".")))
    if not repo:
        sys.exit("error: could not determine the GitHub repository; pass --repo OWNER/NAME")
    args.repo = repo
    return repo


def cmd_fetch(args) -> int:
    issues = _collect_issues(args)
    text = json.dumps(issues, indent=2)
    if args.output:
        Path(args.output).write_text(text)
        print(f"wrote {len(issues)} issue(s) to {args.output}", file=sys.stderr)
    else:
        print(text)
    return 0


def cmd_parse(args) -> int:
    parsed = [parse_issue(raw).to_dict() for raw in _collect_issues(args)]
    print(json.dumps(parsed if len(parsed) != 1 else parsed[0], indent=2))
    return 0


def cmd_triage(args) -> int:
    repo_path = Path(args.repo_path).resolve()
    if git(repo_path, "rev-parse", "--git-dir", check=False).returncode != 0:
        sys.exit(f"error: {repo_path} is not a git repository")
    if args.publish:
        _require_repo(args)  # fail before doing the work, not after
    cfg = config_mod.load(repo_path)
    opts = Options(
        good=args.good, bad=args.bad, test_cmd=args.test_cmd, setup_cmd=args.setup_cmd, match=args.match,
        timeout=args.timeout, sandbox=args.sandbox, trust_issue_code=args.trust_issue_code,
        first_parent=args.first_parent, bisect=not args.no_bisect,
    )
    issues = _collect_issues(args)
    if not issues:
        print("no new issues to triage", file=sys.stderr)
        return 0
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    failures = 0
    for raw in issues:
        number = raw.get("number")
        try:
            result = triage(repo_path, raw, cfg, opts)
        except (GitError, OSError, ValueError) as exc:
            print(f"#{number}: triage failed: {exc}", file=sys.stderr)
            failures += 1
            continue
        report = render(result)
        path = out_dir / f"issue-{number}.md"
        path.write_text(report)
        culprit = result.bisect.culprit.sha if result.bisect and result.bisect.culprit else None
        (out_dir / f"issue-{number}.json").write_text(json.dumps({
            "issue": number, "status": result.status, "bad": result.bad_sha, "good": result.good_ref,
            "culprit": culprit, "suspect": result.suspect.location if result.suspect else None,
            "fix_status": result.fix.status if result.fix else None,
        }, indent=2))
        print(f"#{number}: {result.status}" + (f", introduced by {culprit[:10]}" if culprit else "") + f" -> {path}", file=sys.stderr)
        if len(issues) == 1 and not args.quiet:
            print(report)
        if args.publish:
            url = github.post_comment(args.repo, number, report)
            print(f"#{number}: published {url}", file=sys.stderr)
    if not args.publish:
        print("reports were written locally only (pass --publish to comment on the issues)", file=sys.stderr)
    return 1 if failures else 0


def cmd_publish(args) -> int:
    body = Path(args.report).read_text()
    print(github.post_comment(_require_repo(args), args.issue, body))
    return 0


def _add_issue_source(p: argparse.ArgumentParser, multiple: bool = True) -> None:
    p.add_argument("--repo", help="GitHub repository as OWNER/NAME (default: the origin remote)")
    p.add_argument("--issue", type=int, action="append", help="issue number (repeatable)")
    p.add_argument("--issue-file", help="read issue JSON (object or array) from a file, or '-' for stdin, instead of calling gh")
    p.add_argument("--since", default="24h", help="when no --issue is given: issues opened since this long ago (30m, 24h, 7d) or an ISO timestamp [24h]")
    p.add_argument("--limit", type=int, default=20, help="maximum number of new issues to take [20]")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="oss-bounty-scout", description="Background issue triage: reproduce, bisect, report.")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("fetch", help="fetch newly opened issues as JSON")
    _add_issue_source(p)
    p.add_argument("-o", "--output", help="write JSON here instead of stdout")
    p.set_defaults(func=cmd_fetch)

    p = sub.add_parser("parse", help="show what the parser extracts from issues")
    _add_issue_source(p)
    p.set_defaults(func=cmd_parse)

    p = sub.add_parser("triage", help="reproduce, bisect and write a Markdown report")
    _add_issue_source(p)
    p.add_argument("--repo-path", default=".", help="local clone to test against [.]")
    p.add_argument("--good", help="known-good ref (default: reporter hints, recent tags, then the root commit)")
    p.add_argument("--bad", default="HEAD", help="ref where the bug is present [HEAD]")
    p.add_argument("--test-cmd", help="reproduction command to use instead of the issue's code (exit 0 = good)")
    p.add_argument("--setup-cmd", help="command to run before the test at every commit")
    p.add_argument("--match", help="text that identifies the failure in the output (default: the reported error)")
    p.add_argument("--timeout", type=float, help="seconds allowed per commit [120]")
    p.add_argument("--sandbox", choices=["worktree", "docker"], help="isolation level [worktree]")
    p.add_argument("--trust-issue-code", action="store_true", help="execute code from the issue even when its author is not a maintainer")
    p.add_argument("--first-parent", action="store_true", help="bisect along first-parent history only")
    p.add_argument("--no-bisect", action="store_true", help="stop after the reproduction attempt")
    p.add_argument("--out", default="scout-reports", help="directory for reports [scout-reports]")
    p.add_argument("--quiet", action="store_true", help="do not print the report to stdout")
    p.add_argument("--publish", action="store_true", help="also post each report as a comment on its issue")
    p.set_defaults(func=cmd_triage)

    p = sub.add_parser("publish", help="post an existing report as an issue comment")
    p.add_argument("--repo", help="GitHub repository as OWNER/NAME (default: the origin remote)")
    p.add_argument("--issue", type=int, required=True)
    p.add_argument("--report", required=True, help="Markdown file to post")
    p.set_defaults(func=cmd_publish)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except github.GhError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
