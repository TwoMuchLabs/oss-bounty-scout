import json
import os
import stat
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from bounty_scout import github


class GithubTests(unittest.TestCase):
    def test_parse_since(self):
        now = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(github.parse_since("24h", now), datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc))
        self.assertEqual(github.parse_since("2026-11-01T00:00:00Z"), datetime(2026, 11, 1, tzinfo=timezone.utc))

    def test_new_issues_exclude_pull_requests_and_older_issues(self):
        pages = [[
            {"number": 3, "created_at": "2026-11-20T10:00:00Z"},
            {"number": 2, "created_at": "2026-11-20T09:00:00Z", "pull_request": {}},
            {"number": 1, "created_at": "2026-11-01T09:00:00Z"},  # old issue, recently updated
        ]]
        work = Path(tempfile.mkdtemp())
        fake = work / "gh"
        fake.write_text(f"#!/bin/sh\necho \"$@\" > {work}/args\ncat <<'JSON'\n{json.dumps(pages)}\nJSON\n")
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        old = os.environ["PATH"]
        os.environ["PATH"] = f"{work}{os.pathsep}{old}"
        try:
            issues = github.fetch_new_issues("o/r", datetime(2026, 11, 20, tzinfo=timezone.utc))
        finally:
            os.environ["PATH"] = old
        self.assertEqual([i["number"] for i in issues], [3])
        self.assertIn("repos/o/r/issues", (work / "args").read_text())
        self.assertIn("since=2026-11-20T00:00:00Z", (work / "args").read_text())
