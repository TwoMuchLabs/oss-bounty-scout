import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from bounty_scout import bisect as bisect_mod
from bounty_scout.cli import main
from bounty_scout.config import Config
from bounty_scout.issue_parser import parse_issue
from bounty_scout.repro import (
    BAD, GOOD, SKIP, CheckSpec, build_plan, classify, error_matches, evaluate, missing_names, signature_pattern,
)
from bounty_scout.report import render
from bounty_scout.runner import RunResult, Worktree, docker_argv, git, run_isolated
from bounty_scout.triage import Options, triage

from . import fixture


class RepoCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="scout-test-")
        cls.info = fixture.build(Path(cls._tmp.name) / "statskit")
        cls.repo = cls.info["repo"]

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def worktrees(self):
        return git(self.repo, "worktree", "list", "--porcelain").stdout.count("worktree ")


class RunnerTests(RepoCase):
    def test_worktree_is_isolated_and_removed(self):
        with Worktree(self.repo, "v1.0.0") as wt:
            self.assertTrue((wt.path / "statskit" / "core.py").exists())
            self.assertFalse((wt.path / "statskit" / "later0.py").exists())
            (wt.path / "junk.txt").write_text("x")
            path = wt.path
        self.assertFalse(path.exists())
        self.assertEqual(self.worktrees(), 1)
        self.assertEqual(git(self.repo, "status", "--porcelain").stdout, "")
        self.assertEqual(git(self.repo, "rev-parse", "HEAD").stdout.strip(), self.info["head"])

    def test_environment_is_scrubbed(self):
        os.environ["GH_TOKEN"] = "secret-token"
        os.environ["SCOUT_ALLOWED"] = "yes"
        with Worktree(self.repo, "HEAD") as wt:
            result = run_isolated('echo "token=[$GH_TOKEN] allowed=[$SCOUT_ALLOWED] home=$HOME"', wt.path,
                                  pass_env=["SCOUT_ALLOWED"])
        self.assertIn("token=[] allowed=[yes]", result.output)
        self.assertNotIn(os.environ.get("HOME", "/nonexistent-home") + "\n", result.output)

    def test_timeout_kills_the_process_group(self):
        with Worktree(self.repo, "HEAD") as wt:
            result = run_isolated("sleep 30 & sleep 30", wt.path, timeout=1)
        self.assertTrue(result.timed_out)
        self.assertLess(result.duration, 10)

    def test_setup_failure_is_a_skip(self):
        with Worktree(self.repo, "HEAD") as wt:
            verdict = evaluate(CheckSpec(command="true", setup="exit 3"), wt.path)
        self.assertEqual(verdict.status, SKIP)

    def test_docker_command_has_no_network_and_drops_privileges(self):
        argv = docker_argv("python3 /scout/t.py", "/tmp/wt", "/tmp/repro", "python:3.12-slim")
        self.assertEqual(argv[argv.index("--network") + 1], "none")
        self.assertIn("/tmp/repro:/scout:ro", argv)
        self.assertIn("no-new-privileges", argv)
        self.assertEqual(argv[-3:], ["sh", "-c", "python3 /scout/t.py"])


def _docker_ready() -> bool:
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


@unittest.skipUnless(_docker_ready(), "needs a running Docker daemon")
class DockerTests(RepoCase):
    """The full pipeline inside the real container sandbox."""

    def test_end_to_end_in_container_without_network(self):
        cfg = Config(sandbox="docker", docker_image="python:3.12-slim")
        result = triage(self.repo, fixture.issue(association="NONE"), cfg)  # untrusted author is fine in docker
        self.assertEqual(result.status, "reproduced", result.head and result.head.result.output)
        self.assertEqual(result.bisect.culprit.sha, self.info["culprit"])
        self.assertEqual(result.suspect.location, "statskit/core.py:7")
        self.assertEqual(result.fix.status, "verified")
        self.assertEqual(git(self.repo, "status", "--porcelain").stdout, "")

    def test_container_has_no_network_token_or_root(self):
        os.environ["GH_TOKEN"] = "secret-token"
        probe = ("import os, socket\nprint('uid', os.getuid(), 'token', os.environ.get('GH_TOKEN'))\n"
                 "try:\n    socket.create_connection(('1.1.1.1', 53), 3); print('NETWORK-UP')\n"
                 "except OSError as e:\n    print('network-blocked')\n")
        with tempfile.TemporaryDirectory() as tmp, Worktree(self.repo, "HEAD") as wt:
            os.chmod(tmp, 0o755)
            (Path(tmp) / "probe.py").write_text(probe)
            result = run_isolated("python3 /scout/probe.py", wt.path, sandbox="docker", repro_dir=tmp)
        self.assertIn("token None", result.output)
        self.assertIn("network-blocked", result.output)
        self.assertNotIn("uid 0 ", result.output)

    def test_timeout_stops_the_container(self):
        with Worktree(self.repo, "HEAD") as wt:
            result = run_isolated("sleep 300", wt.path, sandbox="docker", timeout=3)
        self.assertTrue(result.timed_out)


class ClassifyTests(unittest.TestCase):
    def test_signature_decides(self):
        pattern = signature_pattern("IndexError: list index 3 out of range at 0x7f00aa")
        self.assertEqual(classify(RunResult(1, "IndexError: list index 17 out of range at 0x1b", False, 0), pattern)[0], BAD)
        self.assertEqual(classify(RunResult(0, "fine", False, 0), pattern)[0], GOOD)
        self.assertEqual(classify(RunResult(1, "ImportError: nope", False, 0), pattern)[0], SKIP)

    def test_exit_code_decides_without_signature(self):
        self.assertEqual(classify(RunResult(2, "", False, 0), None)[0], BAD)
        self.assertEqual(classify(RunResult(0, "", True, 0), None)[0], SKIP)


class MatchingTests(unittest.TestCase):
    """Cases taken from the field test, where the same bug words its error differently on our side."""

    def test_same_error_reworded_by_a_newer_python(self):
        self.assertTrue(error_matches("TypeError: __init__() got an unexpected keyword argument 'help'", None,
                                      "TypeError: Parameter.__init__() got an unexpected keyword argument 'help'"))
        self.assertTrue(error_matches("TypeError: getattr(): attribute name must be string", None,
                                      "TypeError: attribute name must be string, not 'int'"))

    def test_paths_colour_codes_and_module_prefixes_do_not_matter(self):
        self.assertTrue(error_matches(
            "ImportError: cannot import name 'Markup' from 'jinja2' (/home/u/.venv/lib/python3.9/site-packages/jinja2/__init__.py)",
            None, "ImportError: cannot import name 'Markup' from 'jinja2' (/work/src/jinja2/__init__.py)"))
        self.assertTrue(error_matches("RuntimeError: Foo", None, "RuntimeError: \x1b[31mFoo\x1b[0m"))
        self.assertTrue(error_matches("ParseException: Expected stringEnd", None,
                                      "pyparsing.exceptions.ParseException: Expected stringEnd, found '('"))

    def test_a_different_error_is_not_a_match(self):
        self.assertFalse(error_matches("KeyError: 'a'", None, "NameError: name 'click' is not defined"))
        self.assertFalse(error_matches("TypeError: 'int' object is not iterable", None,
                                       "TypeError: unsupported operand type(s) for +: 'int' and 'str'"))
        self.assertTrue(error_matches("AssertionError", None, "Traceback...\nAssertionError"))


class PlanTests(RepoCase):
    def plan_for(self, body):
        return build_plan(parse_issue(fixture.issue(body=body)))

    def test_snippet_is_run_exactly_as_written(self):
        plan = self.plan_for("```python\nclass A:\n    x: 'B'\n```\n\nNameError: name 'B' is not defined")
        self.assertIn("\nclass A:\n    x: 'B'\n", plan.test_content)
        self.assertNotIn("def test_issue", plan.test_content)

    def test_indented_snippet_is_dedented_and_invalid_blocks_are_skipped(self):
        plan = self.plan_for("```python\n$ python t.py\noutput here\n```\n```python\n    import statskit\n    statskit.mean([])\n```\nZeroDivisionError: division by zero")
        self.assertTrue(plan.snippet.startswith("import statskit"))

    def test_arguments_from_the_report_are_kept(self):
        plan = self.plan_for("```python\nimport sys\nprint(sys.argv)\n```\n```console\n$ python app.py --nme x\nError: No such option: --nme\n```")
        self.assertEqual(plan.command, "python3 {test_file} --nme x")

    def test_missing_names(self):
        self.assertEqual(missing_names("x = mean([1])\nprint(os.getcwd(), x)\nfor i in y: pass"), ["mean", "os", "y"])
        self.assertEqual(missing_names("from m import *\nfoo()"), [])

    def test_forgotten_imports_are_added_from_the_project_and_stdlib(self):
        body = "```python\nprint(os.sep)\nprint(mean([]))\nprint(statskit.spread([1]))\n```\n\nZeroDivisionError: division by zero"
        result = triage(self.repo, fixture.issue(body=body), options=Options(bisect=False))
        self.assertEqual(result.status, "reproduced", result.head.result.output)
        for line in ("import os  #", "from statskit import mean  #", "import statskit  #"):
            self.assertIn(line, result.plan.test_content)
        self.assertIn("Added imports the snippet left out", " ".join(result.notes))

    def test_missing_module_is_explained(self):
        body = "```python\nimport not_installed_anywhere\n```\n\nKeyError: 'x'"
        result = triage(self.repo, fixture.issue(body=body))
        self.assertEqual(result.status, "inconclusive")
        self.assertIn("needs the module `not_installed_anywhere`", " ".join(result.notes))


class BisectOutputTests(unittest.TestCase):
    def test_first_bad_line_from_old_and_new_git(self):
        sha = "96b27637b5e6c46ea13e261418e2192005d7f7fb"
        for line in (f"{sha} is the first bad commit", f"{sha} is the first 'bad' commit"):
            self.assertEqual(bisect_mod._FIRST_BAD.search(f"running x\n{line}\ncommit {sha}").group(1), sha)


class BisectTests(RepoCase):
    CHECK = "python3 -c 'import statskit; statskit.mean([])'"

    def test_finds_first_bad_commit_without_touching_the_checkout(self):
        result = bisect_mod.bisect(self.repo, "v1.1.0", "HEAD", CheckSpec(command=self.CHECK))
        self.assertEqual(result.status, "found", result.detail)
        self.assertEqual(result.culprit.sha, self.info["culprit"])
        self.assertEqual(result.culprit.subject, "Simplify mean()")
        self.assertEqual(result.commits_in_range, 10)
        self.assertLessEqual(len(result.steps), 5)
        self.assertEqual(self.worktrees(), 1)
        self.assertNotEqual(git(self.repo, "bisect", "log", check=False).returncode, 0)  # no bisect state left

    def test_skips_make_the_result_ambiguous(self):
        # Every commit touching util2 or later "cannot be built" (125); the culprit is hidden among them.
        check = "test ! -e statskit/util2.py || test -e statskit/later0.py || exit 125; " + self.CHECK
        result = bisect_mod.bisect(self.repo, "v1.1.0", "HEAD", CheckSpec(command=check, signature_pattern=None))
        # exit 125 from the command is a plain failure without a signature, so use one to get real skips
        self.assertEqual(result.status, "found")
        result = bisect_mod.bisect(self.repo, "v1.1.0", "HEAD",
                                   CheckSpec(command=check, signature_pattern="ZeroDivisionError"))
        self.assertEqual(result.status, "ambiguous")
        self.assertIn(self.info["culprit"], [c.sha for c in result.candidates])

    def test_gives_up_when_too_many_commits_cannot_be_tested(self):
        check = "test ! -e statskit/util0.py || test -e statskit/later5.py || exit 3; " + self.CHECK
        spec = CheckSpec(command=check, signature_pattern="ZeroDivisionError", max_skips=2)
        result = bisect_mod.bisect(self.repo, "v1.1.0", "HEAD", spec)
        self.assertEqual(result.status, "abandoned", result.detail)
        self.assertLessEqual(len(result.steps), 4)
        self.assertIn("could not be tested", result.detail)
        self.assertEqual(self.worktrees(), 1)

    def test_candidate_good_refs_prefer_reporter_hint(self):
        refs = bisect_mod.candidate_good_refs(self.repo, "HEAD", ["1.0.0"])
        self.assertEqual(refs[0], "v1.0.0")
        self.assertEqual(refs[1], "v1.1.0")
        self.assertEqual(len(refs), 3)  # hint, other tag, root commit


class TriageTests(RepoCase):
    def test_end_to_end_regression(self):
        result = triage(self.repo, fixture.issue())
        self.assertEqual(result.status, "reproduced")
        self.assertEqual(result.good_ref, "v1.1.0")
        self.assertEqual(result.bisect.culprit.sha, self.info["culprit"])
        self.assertEqual(result.suspect.location, "statskit/core.py:7")
        self.assertEqual(result.suspect.confidence, "high")
        self.assertEqual((result.fix.scope, result.fix.status), ("file", "verified"))
        self.assertIn("+    if not values:", result.fix.diff)
        self.assertNotIn("CHANGELOG", result.fix.diff)

        report = render(result)
        for expected in ("**Reproduced**", self.info["culprit"][:10], "`statskit/core.py:7`", "```diff",
                         "from statskit import mean\n\nprint(mean([]))", "Ubuntu 22.04", "reported working in `1.1.0`", "Verified:"):
            self.assertIn(expected, report)
        self.assertEqual(self.worktrees(), 1)
        self.assertEqual(git(self.repo, "status", "--porcelain").stdout, "")

    def test_patch_is_merged_when_later_commits_touched_the_same_file(self):
        work = Path(tempfile.mkdtemp(prefix="scout-3way-"))
        info = fixture.build(work / "statskit", commits_after=2)
        core = info["repo"] / "statskit" / "core.py"
        core.write_text(core.read_text().replace("return max(values) - min(values)", "return max(values) - min(values)  # range"))
        subprocess.run(["git", "-C", str(info["repo"]), "-c", "user.name=F", "-c", "user.email=f@e.x",
                        "commit", "-qam", "Comment spread()"], check=True)
        result = triage(info["repo"], fixture.issue())
        self.assertEqual(result.bisect.culprit.sha, info["culprit"])
        self.assertEqual(result.fix.status, "verified")
        self.assertIn("+    if not values:", result.fix.diff)
        self.assertNotIn("# range", "".join(l for l in result.fix.diff.splitlines() if l.startswith(("+", "-"))))

    def test_untrusted_issue_code_is_not_executed(self):
        marker = Path(self._tmp.name) / "pwned"
        body = f"```python\nopen({str(marker)!r}, 'w').write('x')\nraise ValueError('boom')\n```\n\nValueError: boom\n"
        result = triage(self.repo, fixture.issue(association="NONE", body=body))
        self.assertEqual(result.status, "needs-approval")
        self.assertFalse(marker.exists())
        self.assertIn("Waiting for maintainer approval", render(result))
        trusted = triage(self.repo, fixture.issue(association="NONE", body=body), options=Options(trust_issue_code=True, bisect=False))
        self.assertEqual(trusted.status, "reproduced")

    def test_not_reproduced_at_a_good_ref(self):
        result = triage(self.repo, fixture.issue(), options=Options(bad="v1.1.0"))
        self.assertEqual(result.status, "not-reproduced")
        self.assertIsNone(result.bisect)

    def test_different_failure_is_inconclusive(self):
        body = "```python\nimport statskit\nstatskit.nope()\n```\n\n```\nKeyError: 'something else'\n```"
        result = triage(self.repo, fixture.issue(body=body))
        self.assertEqual(result.status, "inconclusive")

    def test_no_runnable_reproduction(self):
        result = triage(self.repo, fixture.issue(body="It crashes sometimes."))
        self.assertEqual(result.status, "needs-info")
        self.assertIn("Needs more information", render(result))

    def test_maintainer_test_command_and_config(self):
        cfg = Config(good="v1.0.0", timeout=30)
        opts = Options(test_cmd="python3 -c 'import statskit; statskit.mean([])'")
        result = triage(self.repo, fixture.issue(association="NONE", body="mean of nothing fails"), cfg, opts)
        self.assertEqual(result.status, "reproduced")
        self.assertEqual(result.good_ref, "v1.0.0")
        self.assertEqual(result.bisect.culprit.sha, self.info["culprit"])
        self.assertEqual(result.suspect.file, "statskit/core.py")
        self.assertEqual(result.fix.status, "verified")

    def test_bug_present_since_the_first_commit(self):
        opts = Options(test_cmd="python3 -c 'import statskit; statskit.spread([])'")
        result = triage(self.repo, fixture.issue(body="spread([]) fails"), options=opts)
        self.assertEqual(result.status, "reproduced")
        self.assertIsNone(result.bisect)
        self.assertIn("No known-good commit was found", result.notes[0])


class CliTests(RepoCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="scout-cli-"))
        self.calls = self.work / "gh-calls.log"
        fake = self.work / "bin" / "gh"
        fake.parent.mkdir()
        fake.write_text(f'#!/bin/sh\necho "$@" >> {self.calls}\ncat > {self.work}/gh-stdin\necho https://example/comment/1\n')
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        self._path = os.environ["PATH"]
        os.environ["PATH"] = f"{fake.parent}{os.pathsep}{self._path}"
        self.issue_file = self.work / "issue.json"
        import json
        self.issue_file.write_text(json.dumps(fixture.issue()))

    def tearDown(self):
        os.environ["PATH"] = self._path

    def run_cli(self, *extra):
        return main(["triage", "--repo-path", str(self.repo), "--issue-file", str(self.issue_file),
                     "--out", str(self.work / "out"), "--quiet", *extra])

    def test_nothing_is_posted_without_publish(self):
        self.assertEqual(self.run_cli(), 0)
        self.assertTrue((self.work / "out" / "issue-42.md").exists())
        self.assertFalse(self.calls.exists())

    def test_publish_posts_the_report_as_a_comment(self):
        self.assertEqual(self.run_cli("--publish", "--repo", "example/statskit"), 0)
        self.assertEqual(self.calls.read_text().strip(), "issue comment 42 --repo example/statskit --body-file -")
        self.assertEqual((self.work / "gh-stdin").read_text(), (self.work / "out" / "issue-42.md").read_text())

    def test_installed_entry_point_help(self):
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
        out = subprocess.run(["python3", "-m", "bounty_scout", "triage", "--help"], capture_output=True, text=True, env=env)
        self.assertEqual(out.returncode, 0)
        self.assertIn("--publish", out.stdout)


if __name__ == "__main__":
    unittest.main()
