"""Field test: replay closed issues from a public project as if each had just been opened.

For every closed issue the parser can turn into a runnable reproduction, the
scout is run at the commit that was current when the issue was opened. Two
things the project's own history tells us are then used to score it:

* the commit that closed the issue: the reproduction should fail before the
  issue was opened and pass once the maintainers' fix is in;
* the files that fix touched: the scout's suspect file should be one of them.

    field_test.py fetch OWNER/NAME issues.json [--issues 300]     # needs GH_TOKEN
    field_test.py run   REPO_PATH issues.json OUT_DIR --image IMAGE   # no token
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from bounty_scout.config import Config
from bounty_scout.issue_parser import parse_issue
from bounty_scout.report import render
from bounty_scout.repro import build_plan
from bounty_scout.runner import git
from bounty_scout.triage import Options, triage


def gh(path: str):
    out = subprocess.run(["gh", "api", path], capture_output=True, text=True)
    if out.returncode != 0:
        print(f"gh api {path}: {out.stderr.strip()[:200]}", file=sys.stderr)
        return None
    return json.loads(out.stdout)


def is_candidate(raw: dict, prose: bool = False) -> bool:
    """Default: reports with a runnable snippet and an error. ``prose``: an error but nothing runnable (the AI's job)."""
    issue = parse_issue(raw)
    plan = build_plan(issue)
    if prose:
        return bool(plan is None and issue.error_signature)
    return bool(plan and plan.source == "python-snippet" and issue.error_signature)


def cmd_fetch(args) -> int:
    issues = []
    for page in range(1, args.pages + 1):  # the endpoint mixes in pull requests, so pages are mostly not issues
        batch = gh(f"repos/{args.repo}/issues?state=closed&sort=created&direction=desc&per_page=100&page={page}") or []
        issues += [i for i in batch if "pull_request" not in i and i.get("state_reason") in (None, "completed")]
        if not batch or len(issues) >= args.issues:
            break
    issues = issues[: args.issues]
    candidates = [i for i in issues if is_candidate(i, args.prose)]
    for raw in candidates:
        n = raw["number"]
        timeline = gh(f"repos/{args.repo}/issues/{n}/timeline?per_page=100") or []
        fix, thread = None, [raw.get("body") or ""]
        closed_at = raw.get("closed_at")
        for event in timeline:
            if event.get("event") == "closed" and event.get("commit_id"):
                fix = event["commit_id"]
            if event.get("event") == "commented":
                thread.append(event.get("body") or "")
            if event.get("event") == "cross-referenced":
                source = (event.get("source") or {}).get("issue") or {}
                thread.append(source.get("body") or "")
                pr = source.get("pull_request") or {}
                if not fix and pr.get("merged_at") and closed_at and source.get("repository", {}).get("full_name", "").lower() == args.repo.lower():
                    gap = abs(_ts(pr["merged_at"]) - _ts(closed_at))
                    if gap < timedelta(minutes=10):
                        detail = gh(f"repos/{args.repo}/pulls/{source['number']}") or {}
                        fix = detail.get("merge_commit_sha")
        raw["_fix_commit"], raw["_thread"] = fix, "\n".join(thread)
    Path(args.output).write_text(json.dumps({"repo": args.repo, "scanned": len(issues), "candidates": candidates}))
    print(f"{args.repo}: scanned {len(issues)} closed issues, {len(candidates)} with a runnable snippet and an error", file=sys.stderr)
    return 0


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _commit_before(repo: Path, when: str) -> str | None:
    return git(repo, "rev-list", "-1", f"--before={when}", "HEAD").stdout.strip() or None


def _files(repo: Path, sha: str) -> list[str]:
    return git(repo, "show", "--format=", "--name-only", "--first-parent", sha, check=False).stdout.split()


def cmd_run(args) -> int:
    repo, out = Path(args.repo_path).resolve(), Path(args.out)
    (out / "reports").mkdir(parents=True, exist_ok=True)
    data = json.loads(Path(args.issues).read_text())
    cfg = Config(sandbox=args.sandbox, docker_image=args.image, timeout=args.timeout, ai=args.ai, ai_model=args.ai_model)
    rows = []
    for raw in data["candidates"][: args.limit]:
        n, started = raw["number"], time.monotonic()
        row = {"issue": n, "title": raw["title"], "url": raw.get("html_url"), "fix_commit": raw.get("_fix_commit")}
        try:
            bad = _commit_before(repo, raw["created_at"])
            if not bad:
                row["status"] = "no-commit-at-issue-time"
                rows.append(row)
                continue
            result = triage(repo, raw, cfg, Options(bad=bad, trust_issue_code=args.sandbox == "worktree"))
            (out / "reports" / f"issue-{n}.md").write_text(render(result))
            row.update(status=result.status, bad=bad, good=result.good_ref,
                       signature=result.issue.error_signature,
                       head_reason=result.head.reason if result.head else None,
                       head_tail=result.head.result.output.strip().splitlines()[-2:] if result.head else None)
            b = result.bisect
            if b:
                row.update(bisect=b.status, range=b.commits_in_range, steps=len(b.steps))
            if b and b.culprit:
                c = b.culprit
                pr = re.search(r"#(\d+)", c.subject)
                thread = raw.get("_thread") or ""
                row.update(culprit=c.sha, culprit_subject=c.subject, culprit_date=c.date,
                           culprit_files=_files(repo, c.sha)[:12],
                           culprit_in_thread=bool(c.sha[:7] in thread or (pr and re.search(rf"#{pr.group(1)}\b", thread))))
            if result.suspect:
                row.update(suspect=result.suspect.location, confidence=result.suspect.confidence)
            if result.fix:
                row.update(patch=result.fix.status, patch_scope=result.fix.scope)
            row.update(ai_calls=result.ai_calls, ai_test=result.ai_test_gate,
                       test_source=result.plan.source if result.plan else None)

            # Score against what the maintainers actually did.
            fix = raw.get("_fix_commit")
            if fix and git(repo, "cat-file", "-e", f"{fix}^{{commit}}", check=False).returncode != 0:
                fix = None
            after = fix or (raw.get("closed_at") and _commit_before(repo, (_ts(raw["closed_at"]) + timedelta(minutes=5)).isoformat()))
            if result.status == "reproduced" and after and after != bad:
                recheck = triage(repo, raw, cfg, Options(bad=after, bisect=False, trust_issue_code=args.sandbox == "worktree"))
                row["after_fix"] = recheck.status  # "not-reproduced" means the real fix makes our test pass
            if fix:
                fix_files = _files(repo, fix)
                row["fix_files"] = fix_files[:12]
                if result.suspect:
                    row["suspect_in_fix"] = result.suspect.file in fix_files
        except Exception as exc:  # keep going: one odd issue must not sink the batch
            row["status"] = "error"
            row["error"] = f"{type(exc).__name__}: {exc}"[:300]
        row["seconds"] = round(time.monotonic() - started, 1)
        rows.append(row)
        print("ROW " + json.dumps(row), flush=True)  # the log is the record; artifacts carry the full reports

    summary = summarise(data, rows)
    (out / "results.json").write_text(json.dumps({"repo": data["repo"], "summary": summary, "rows": rows}, indent=2))
    (out / "summary.md").write_text(markdown(data["repo"], summary, rows))
    print("SUMMARY " + json.dumps({"repo": data["repo"], **summary}))
    return 0


def summarise(data: dict, rows: list[dict]) -> dict:
    count = lambda pred: sum(1 for r in rows if pred(r))
    return {
        "closed_issues_scanned": data["scanned"],
        "runnable": len(rows),
        "reproduced": count(lambda r: r.get("status") == "reproduced"),
        "not_reproduced": count(lambda r: r.get("status") == "not-reproduced"),
        "inconclusive": count(lambda r: r.get("status") == "inconclusive"),
        "errors": count(lambda r: r.get("status") == "error"),
        "reproduced_and_real_fix_passes": count(lambda r: r.get("after_fix") == "not-reproduced"),
        "reproduced_but_still_fails_after_fix": count(lambda r: r.get("after_fix") == "reproduced"),
        "culprit_found": count(lambda r: r.get("bisect") == "found"),
        "culprit_ambiguous": count(lambda r: r.get("bisect") == "ambiguous"),
        "bisect_abandoned": count(lambda r: r.get("bisect") in ("abandoned", "failed")),
        "no_good_baseline": count(lambda r: r.get("status") == "reproduced" and not r.get("bisect")),
        "culprit_named_in_thread": count(lambda r: r.get("culprit_in_thread")),
        "suspect_file_scored": count(lambda r: "suspect_in_fix" in r),
        "suspect_file_matches_fix": count(lambda r: r.get("suspect_in_fix")),
        "patch_verified": count(lambda r: r.get("patch") == "verified"),
        "ai_test_accepted": count(lambda r: r.get("test_source") == "ai-test"),
        "ai_patch_verified": count(lambda r: r.get("patch_scope") == "ai" and r.get("patch") == "verified"),
        "ai_calls": sum(r.get("ai_calls") or 0 for r in rows),
    }


def markdown(repo: str, s: dict, rows: list[dict]) -> str:
    lines = [f"## Field test: {repo}", "", "| | |", "|---|---|"] + [f"| {k.replace('_', ' ')} | {v} |" for k, v in s.items()]
    lines += ["", "| issue | status | culprit | suspect | after real fix | suspect in fix |", "|---|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| #{r['issue']} | {r.get('status')} | {(r.get('culprit') or r.get('bisect') or '')[:10]} | "
                     f"{r.get('suspect', '')} | {r.get('after_fix', '')} | {r.get('suspect_in_fix', '')} |")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("fetch")
    p.add_argument("repo"); p.add_argument("output"); p.add_argument("--pages", type=int, default=30)
    p.add_argument("--issues", type=int, default=300, help="closed issues to scan")
    p.add_argument("--prose", action="store_true", help="select reports with an error but no runnable code (for --ai runs)")
    p.set_defaults(func=cmd_fetch)
    p = sub.add_parser("run")
    p.add_argument("repo_path"); p.add_argument("issues"); p.add_argument("out")
    p.add_argument("--image", required=True); p.add_argument("--limit", type=int, default=40)
    p.add_argument("--timeout", type=float, default=60)
    p.add_argument("--sandbox", default="docker", choices=["docker", "worktree"])
    p.add_argument("--ai", default="off", choices=["off", "anthropic"]); p.add_argument("--ai-model")
    p.set_defaults(func=cmd_run)
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
