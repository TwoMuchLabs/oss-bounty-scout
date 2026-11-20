import os
import shutil
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

    def test_packages_are_baked_into_a_derived_image(self):
        body = "```python\nimport six\nimport statskit\nstatskit.mean([])\n```\n\nZeroDivisionError: division by zero"
        cfg = Config(sandbox="docker", docker_packages=["six"])
        result = triage(self.repo, fixture.issue(body=body), cfg, Options(bisect=False))
        self.assertEqual(result.status, "reproduced", result.head.result.output)
        without = triage(self.repo, fixture.issue(body=body), Config(sandbox="docker"), Options(bisect=False))
        self.assertEqual(without.status, "inconclusive")
        self.assertIn("needs the module `six`", " ".join(without.notes))

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

    def test_pytest_assert_details_and_reordered_dicts(self):
        self.assertTrue(error_matches("AssertionError: assert '' == 'Info log from the default logger.'", None,
                                      "Traceback ...\nAssertionError"))
        self.assertFalse(error_matches("ValueError: bad value given", None, "ValueError"))
        want = "ValidationError: {'iban_data': {'auto_withdrawal_bank_account_id': ['Missing data for required field.'], 'deposit_bank_account_id': ['Missing data for required field.']}}"
        got = "marshmallow.exceptions.ValidationError: {'iban_data': {'deposit_bank_account_id': ['Missing data for required field.'], 'auto_withdrawal_bank_account_id': ['Missing data for required field.']}}"
        self.assertTrue(error_matches(want, None, got))

    def test_false_positives_found_in_the_field(self):
        # a different name is a different error (tqdm #872, isort #2723)
        self.assertFalse(error_matches("NameError: name 'IProgress' is not defined", None, "NameError: name 'tfds' is not defined"))
        # ...but an apostrophe in prose is not a quoted name (jinja #1612)
        pickle = "_pickle.PicklingError: Can't pickle <function sync_do_first at 0x{}>: it's not the same object as jinja2.filters.sync_do_first"
        self.assertTrue(error_matches(pickle.format("7f1a2b3c4d50"), None, pickle.format("7869cc3c4680")))
        self.assertFalse(error_matches("ModuleNotFoundError: No module named 'os.parse'; 'os' is not a package", None,
                                       "ModuleNotFoundError: No module named 'http.parse'"))
        # text that merely ends in "Exception" is not an exception (pygments #1816)
        self.assertIsNone(parse_issue(fixture.issue(body="```\nToken.Name.Exception\nToken.Text\n```")).error_signature)
        self.assertFalse(error_matches("Token.Name.Exception", None, "Token.Name.Exception\nToken.Text.Whitespace"))
        # logging output is not an error report (tqdm #1581)
        self.assertIsNone(parse_issue(fixture.issue(body="```\nERROR:main:outside redirect -- filter\n```")).error_signature)
        self.assertEqual(parse_issue(fixture.issue(body="```\nerror: unknown option '--x'\n```")).error_signature,
                         "error: unknown option '--x'")

    def test_once_the_reported_line_is_seen_look_alikes_stop_counting(self):
        want = "ParseException: Expected identifier, found '1'"
        pattern = signature_pattern(want)
        fixed = RunResult(1, "pyparsing.exceptions.ParseException: Expected custom identifier, found '1'", False, 0)
        self.assertEqual(classify(fixed, pattern, want)[0], BAD)  # tolerant: could be the same bug, reworded
        self.assertEqual(classify(fixed, pattern, want, strict=True)[0], SKIP)  # strict: the reported message is gone

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

    def test_common_names_and_submodules_are_imported(self):
        from bounty_scout.repro import resolve_imports
        code = "p = Path('.')\nx: Optional[int] = None\nprint(datetime.now(), os.sep, pkg.sub.thing, time())"
        lines = resolve_imports(missing_names(code), {"pkg"}, {"thing": [("pkg.sub", True)]}, code)
        self.assertEqual(lines, ["from pathlib import Path", "from typing import Optional", "from datetime import datetime",
                                 "import os", "import pkg", "import pkg.sub", "from time import time"])
        # a project that exports the same name wins over the standard-library guess
        self.assertEqual(resolve_imports(["Path"], {"pkg"}, {"Path": [("pkg", True)]}, "Path()"), ["from pkg import Path"])

    def test_snippet_that_only_defines_tests_gets_them_called(self):
        body = "```python\nfrom statskit import mean\n\ndef test_empty():\n    assert mean([]) == 0.0\n```\n\n```\nFAILED t.py::test_empty - ZeroDivisionError: division by zero\n```"
        issue = parse_issue(fixture.issue(body=body))
        self.assertEqual(issue.error_signature, "ZeroDivisionError: division by zero")
        result = triage(self.repo, fixture.issue(body=body), options=Options(bisect=False))
        self.assertEqual(result.status, "reproduced", result.head.result.output)
        self.assertIn("test_empty()\n", result.plan.test_content)

    def test_tests_that_need_fixtures_are_run_with_pytest(self):
        plan = self.plan_for("```python\ndef test_x(tmp_path):\n    assert 0\n```\n\n```\nFAILED t.py::test_x - assert 0\n```")
        self.assertEqual(plan.command, "python3 -m pytest -q -x -p no:cacheprovider {test_file}")
        self.assertEqual(plan.signature, "AssertionError: assert 0")
        # without pytest output in the report, a function that merely looks like a test is left alone
        quiet = self.plan_for("```python\ndef test_x(tmp_path):\n    assert 0\nraise KeyError('k')\n```\n\nKeyError: 'k'")
        self.assertEqual(quiet.command, "python3 {test_file}")
        own = self.plan_for("```python\ndef test_x():\n    assert 0\ntest_x()\n```\n\nAssertionError")
        self.assertEqual(own.command, "python3 {test_file}")
        self.assertNotIn("# added", own.test_content)

    def test_ipython_transcript_and_version_boilerplate(self):
        body = ("```python\nimport statskit, sys\nprint(statskit.__name__, sys.version, sys.platform)\n```\n\n"
                "```\nIn [1]: from statskit import mean\n\nIn [2]: for x in [[]]:\n   ...:     mean(x)\n   ...:\n"
                "---------------------------------------------------------------------------\n"
                "ZeroDivisionError                         Traceback (most recent call last)\n"
                "ZeroDivisionError: division by zero\n```")
        plan = self.plan_for(body)
        self.assertEqual(plan.snippet.strip(), "from statskit import mean\nfor x in [[]]:\n    mean(x)")
        result = triage(self.repo, fixture.issue(body=body), options=Options(bisect=False))
        self.assertEqual(result.status, "reproduced", result.head.result.output)

    def test_error_that_never_touches_the_project_is_flagged(self):
        body = "```python\nimport statskit\nimport random\nrandom.sample(range(3), k=9)\n```\n\nValueError: Sample larger than population or is negative"
        result = triage(self.repo, fixture.issue(body=body), options=Options(bisect=False))
        self.assertEqual(result.status, "reproduced")
        self.assertIn("without passing through the project's own files", " ".join(result.notes))
        clean = triage(self.repo, fixture.issue(), options=Options(bisect=False))
        self.assertNotIn("without passing through", " ".join(clean.notes))

    def test_missing_module_is_explained(self):
        body = "```python\nimport not_installed_anywhere\n```\n\nKeyError: 'x'"
        result = triage(self.repo, fixture.issue(body=body))
        self.assertEqual(result.status, "inconclusive")
        self.assertIn("needs the module `not_installed_anywhere`", " ".join(result.notes))


@unittest.skipUnless(shutil.which("node"), "needs Node.js")
class NodeTests(unittest.TestCase):
    """JavaScript snippets: require('<package name>') must reach the checkout under test."""

    @classmethod
    def setUpClass(cls):
        cls.work = Path(tempfile.mkdtemp(prefix="scout-node-"))
        cls.repo = cls.work / "tinylib"
        good = "exports.size = function (s) {\n  if (s == null) return 0;\n  return s.length;\n};\n"
        fixture._git(cls.work, "init", "-q", "-b", "main", str(cls.repo))
        fixture._commit(cls.repo, "start", {"package.json": '{"name": "tinylib", "main": "index.js"}\n', "index.js": good,
                                            "lib/extra.js": "exports.two = () => 2;\n"})
        fixture._git(cls.repo, "tag", "v1.0.0")
        fixture._commit(cls.repo, "docs", {"README.md": "# tinylib\n"})
        cls.culprit = fixture._commit(cls.repo, "simplify size", {"index.js": good.replace("  if (s == null) return 0;\n", "")})
        fixture._commit(cls.repo, "more docs", {"README.md": "# tinylib\n\nmore\n"})

    BODY = ("Since the last release this crashes:\n\n```js\nconst tiny = require('tinylib');\n"
            "const { two } = require('tinylib/lib/extra');\nconsole.log(two(), tiny.size(undefined));\n```\n\n"
            "```\nTypeError: Cannot read properties of undefined (reading 'length')\n    at exports.size (/home/me/node_modules/tinylib/index.js:2:12)\n```\n")

    def test_end_to_end(self):
        result = triage(self.repo, fixture.issue(body=self.BODY))
        self.assertEqual(result.status, "reproduced", result.head and result.head.result.output)
        self.assertEqual(result.plan.source, "node-snippet")
        self.assertEqual(result.bisect.culprit.sha, self.culprit)
        self.assertEqual(result.suspect.file, "index.js")
        self.assertEqual(result.fix.status, "verified")
        self.assertEqual(git(self.repo, "status", "--porcelain").stdout, "")

    def test_import_style_snippet(self):
        body = self.BODY.replace("const tiny = require('tinylib');", "import tiny from 'tinylib';").replace(
            "const { two } = require('tinylib/lib/extra');", "import extra from 'tinylib/lib/extra.js';\nconst { two } = extra;")
        result = triage(self.repo, fixture.issue(body=body))
        self.assertTrue(result.plan.test_filename.endswith(".mjs"))
        self.assertEqual(result.status, "reproduced", result.head and result.head.result.output)
        self.assertEqual(result.bisect.culprit.sha, self.culprit)

    def test_unlabelled_javascript_is_recognised(self):
        body = self.BODY.replace("```js", "```")
        self.assertEqual(parse_issue(fixture.issue(body=body)).code_blocks[0].kind, "javascript")


class ImageChoiceTests(RepoCase):
    def pick(self, body, **cfg):
        from bounty_scout.triage import TriageResult, _sandbox_image
        issue = parse_issue(fixture.issue(body=body))
        result = TriageResult(issue, "needs-info", bad_sha=self.info["head"])
        plan = build_plan(issue) or build_plan(parse_issue(fixture.issue()))
        return _sandbox_image(self.repo, result, issue, plan, Config(**cfg)), result.notes

    def test_python_version_follows_the_report(self):
        snippet = "```python\nimport statskit\n```\n\nKeyError: 'x'\n\n"
        image, notes = self.pick(snippet + "Python 3.9.7 on Ubuntu", docker_image="ci:py{python}")
        self.assertEqual((image, notes), ("ci:py3.9", ["Ran on Python 3.9, to match the version in the report."]))
        self.assertEqual(self.pick(snippet + "Python 3.6", docker_image="ci:py{python}")[0], "ci:py3.8")  # nearest we have
        self.assertEqual(self.pick(snippet, docker_image="ci:py{python}"), ("ci:py3.12", []))  # recent commit: default
        self.assertEqual(self.pick(snippet + "Python 3.9", docker_image="fixed:latest")[0], "fixed:latest")

    def test_javascript_gets_a_node_image_unless_one_was_chosen(self):
        js = "```js\nconst x = require('x');\n```\n\nTypeError: boom"
        self.assertEqual(self.pick(js)[0], "node:22-slim")
        self.assertEqual(self.pick(js, docker_image="mine:1")[0], "mine:1")


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

        self.assertEqual((result.match, result.in_project, result.regression), ("exact", True, "yes"))
        report = render(result)
        self.assertIn("| **Confidence** | **High**: an earlier version (`v1.1.0`) passes this test", report)
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
        self.assertEqual(result.regression, "no")
        self.assertIn("Probably not a regression", result.notes[0])
        self.assertIn("Probably not: already fails at", render(result))
        self.assertIn("| **Confidence** | **Low**: every version that could be tested fails the same way", render(result))

    def test_regression_is_unknown_when_no_older_version_can_run(self):
        # The helper module only exists from the culprit onwards, so every older version fails differently.
        work = Path(tempfile.mkdtemp(prefix="scout-unk-"))
        repo = work / "r"
        fixture._git(work, "init", "-q", "-b", "main", str(repo))
        fixture._commit(repo, "one", {"pkg/__init__.py": ""})
        fixture._commit(repo, "two", {"pkg/tool.py": "def run():\n    raise KeyError('boom')\n"})
        body = "```python\nfrom pkg.tool import run\nrun()\n```\n\nKeyError: 'boom'"
        result = triage(repo, fixture.issue(body=body))
        self.assertEqual((result.status, result.regression), ("reproduced", "unknown"))
        self.assertIn("No earlier version could be tested", " ".join(result.notes))

    def test_one_hunk_of_the_culprit_is_undone_when_the_whole_file_no_longer_reverts(self):
        work = Path(tempfile.mkdtemp(prefix="scout-hunk-"))
        repo = work / "r"
        filler = "".join(f"# line {i}\n" for i in range(30))
        good = "def mean(values):\n    if not values:\n        return 0.0\n    return sum(values) / len(values)\n\n" + filler + "LIMIT = 10\n"
        fixture._git(work, "init", "-q", "-b", "main", str(repo))
        fixture._commit(repo, "start", {"pkg/__init__.py": "from .core import mean\n", "pkg/core.py": good})
        fixture._git(repo, "tag", "v1")
        broken = good.replace("    if not values:\n        return 0.0\n", "").replace("LIMIT = 10", "LIMIT = 20")
        culprit = fixture._commit(repo, "tidy", {"pkg/core.py": broken})
        fixture._commit(repo, "raise limit", {"pkg/core.py": broken.replace("LIMIT = 20", "LIMIT = 30")})
        body = "```python\nfrom pkg import mean\nmean([])\n```\n\nZeroDivisionError: division by zero"
        result = triage(repo, fixture.issue(body=body))
        self.assertEqual(result.bisect.culprit.sha, culprit)
        self.assertEqual((result.fix.scope, result.fix.status), ("hunk", "verified"))
        self.assertIn("+    if not values:", result.fix.diff)
        self.assertNotIn("LIMIT", result.fix.diff)
        self.assertIn("undoes one part of the culprit's change", render(result))

    def test_type_checker_reports_are_not_run(self):
        body = "```python\nimport statskit\nx: int = statskit.mean([1])\n```\n\n```\nt.py:2: error: Incompatible types in assignment  [assignment]\n```\nmypy 1.8"
        result = triage(self.repo, fixture.issue(body=body))
        self.assertEqual(result.status, "unsupported")
        self.assertIn("Not something this tool can run", render(result))
        with_cmd = triage(self.repo, fixture.issue(body=body), options=Options(test_cmd="true", bisect=False))
        self.assertEqual(with_cmd.status, "not-reproduced")


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
