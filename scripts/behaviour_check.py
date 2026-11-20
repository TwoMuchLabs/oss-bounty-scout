#!/usr/bin/env python3
"""Does a change to the code change what the tool does? Measure it before pushing a refactor.

    python scripts/behaviour_check.py <base-ref> [--issues issues.json.gz] [--fetch fetch.json.gz]
                                      [--repo PATH ...] [--quick]

Runs the code from <base-ref> and the code in the working tree over the same inputs and
compares everything they produce. A clean result is evidence, not proof.

What one snapshot covers:
  parse     every recorded issue, plus damaged copies of it (fences without a language,
            indented code, a body cut off mid-way, bold headings, Windows line endings)
  plan      the reproduction plan for each issue: as written, with a maintainer command,
            with --match; and the imports added from three real project trees
  blocks    each Python snippet: test-function handling, unbound names, import lines
  match     every reported error against its own log and against other issues' logs,
            strict and not, and the verdict for exit codes, time-outs and failed set-up
  index     the importable names of each project tree given with --repo
  frames    traceback paths mapped onto each repository's files
  locate    the suspect line for recent commits of each repository, given stack frames
            on, near and away from the commit's changes
  triage    the whole pipeline on small repositories built for the purpose: regression,
            no regression, unknown, not reproduced, inconclusive, no code, untrusted
            author, maintainer command, type-checker report, missing imports, missing
            module, one-hunk revert, three-way merge, AI-written test and patch
  report    the Markdown report for thousands of made-up results, so that every row,
            note and section is rendered in every combination
  cli       `triage` from the command line: one issue, several, a failure, --publish
  field     the field test: `fetch` replayed from recorded GitHub answers, and `run`
            with made-up triage results against a real repository's history

--quick skips triage and cli (the slow parts); --only text,reports runs just the parts
named (text covers parse, plan, blocks and match; repositories covers index, frames and
locate). Without --issues the parser sees only
the issues in the test suite; without --fetch the field test's fetch is skipped.

Not covered: Docker, real AI providers, posting to GitHub, and anything the recorded
issues do not contain.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import gzip
import hashlib
import io
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VOLATILE = [
    (re.compile(r"\b\d+\.\d+s\b"), "N.Ns"),
    (re.compile(r'"(seconds|duration)": [\d.]+'), r'"\1": 0'),
    (re.compile(r"(scout-[a-z0-9]+-)[A-Za-z0-9_]{6,}"), r"\1X"),
    (re.compile(r"/tmp/tmp[A-Za-z0-9_]{6,}"), "/tmp/tmpX"),
]

STUB = """import pathlib, sys
box = pathlib.Path(sys.argv[1])
n = len(list(box.glob('prompt-*.txt'))) + 1
(box / f'prompt-{n}.txt').write_text(sys.stdin.read())
reply = box / f'reply-{n}.txt'
sys.stdout.write(reply.read_text() if reply.exists() else 'no idea')
"""
PROSE = "Calling mean on an empty list crashes since the last update. It used to return zero.\n\nZeroDivisionError: division by zero\n"
GOOD_TEST = "```python\nfrom statskit import mean\n\nmean([])\n```"
WRONG_TEST = "```python\nimport statskit\nstatskit.does_not_exist()\n```"
SPREAD_FIX = ("FILE: statskit/core.py\n<<<<<<< SEARCH\n    return max(values) - min(values)\n=======\n"
              "    if not values:\n        return 0.0\n    return max(values) - min(values)\n>>>>>>> REPLACE")
SPREAD_BODY = "```python\nimport statskit\nstatskit.spread([])\n```\n\nIt raises:\n\n```\nValueError\n```\n"
MEAN = "```python\nfrom statskit import mean\nmean([])\n```\n\nZeroDivisionError: division by zero"

# (name, repository, issue fields, config fields, option fields, canned AI replies)
SCENARIOS = [
    ("regression", "statskit", {}, {}, {}, None),
    ("regression-strict-first-parent", "statskit", {}, {}, {"strict": True, "first_parent": True}, None),
    ("regression-good-given", "statskit", {}, {}, {"good": "v1.0.0"}, None),
    ("regression-no-bisect", "statskit", {}, {}, {"bisect": False}, None),
    ("untrusted", "statskit", {"association": "NONE"}, {}, {}, None),
    ("untrusted-trusted-by-flag", "statskit", {"association": "NONE"}, {}, {"trust_issue_code": True, "bisect": False}, None),
    ("not-reproduced", "statskit", {}, {}, {"bad": "v1.1.0"}, None),
    ("inconclusive", "statskit", {"body": "```python\nimport statskit\nstatskit.nope()\n```\n\n```\nKeyError: 'something else'\n```"}, {}, {}, None),
    ("missing-module", "statskit", {"body": "```python\nimport nosuchmod_xyz\n```\n\nKeyError: 'k'"}, {}, {}, None),
    ("no-code", "statskit", {"body": "It crashes sometimes."}, {}, {}, None),
    ("maintainer-command", "statskit", {"association": "NONE", "body": "mean of nothing fails"}, {"good": "v1.0.0", "timeout": 30},
     {"test_cmd": "python3 -c 'import statskit; statskit.mean([])'"}, None),
    ("maintainer-command-match", "statskit", {"body": "mean of nothing fails"}, {},
     {"test_cmd": "python3 -c 'import statskit; statskit.mean([])'", "match": "ZeroDivisionError"}, None),
    ("config-test-template", "statskit", {}, {"test": "python3 -B {test_file}"}, {}, None),
    ("always-failed", "statskit", {"body": "spread([]) fails"}, {}, {"test_cmd": "python3 -c 'import statskit; statskit.spread([])'"}, None),
    ("type-check", "statskit", {"body": "```python\nimport statskit\nx: int = statskit.mean([1])\n```\n\n```\nt.py:2: error: Incompatible types in assignment  [assignment]\n```\nmypy 1.8"}, {}, {}, None),
    ("type-check-with-command", "statskit", {"body": "```python\nx: int = 1\n```\n\n```\nt.py:2: error: Incompatible types  [assignment]\n```\nmypy 1.8"}, {}, {"test_cmd": "true", "bisect": False}, None),
    ("forgotten-imports", "statskit", {"body": "```python\nprint(mean([]))\n```\n\nZeroDivisionError: division by zero"}, {}, {}, None),
    ("outside-project", "statskit", {"body": "```python\nimport statskit\nraise ValueError('boom')\n```\n\nValueError: boom"}, {}, {}, None),
    ("similar-message", "statskit", {"body": "```python\nfrom statskit import mean\nmean([])\n```\n\nZeroDivisionError: float division by zero"}, {}, {}, None),
    ("type-only", "statskit", {"body": "```python\nfrom statskit import mean\nmean([])\n```\n\n```\nZeroDivisionError\n```"}, {}, {}, None),
    ("defines-tests", "statskit", {"body": "```python\nfrom statskit import mean\n\ndef test_empty():\n    assert mean([]) == 0.0\n```\n\n```\nFAILED t.py::test_empty - ZeroDivisionError: division by zero\n```"}, {}, {}, None),
    ("shell-steps", "statskit", {"body": "### Steps to reproduce\n\n```sh\n$ pip install statskit\n$ python3 -c 'import statskit; statskit.mean([])'\n```\n\nZeroDivisionError: division by zero"}, {}, {}, None),
    ("unknown-regression", "unknown", {"body": "```python\nfrom pkg.tool import run\nrun()\n```\n\nKeyError: 'boom'"}, {}, {}, None),
    ("one-hunk", "hunk", {"body": "```python\nfrom pkg import mean\nmean([])\n```\n\nZeroDivisionError: division by zero"}, {}, {}, None),
    ("three-way", "threeway", {}, {}, {}, None),
    ("ai-test", "statskit", {"body": PROSE}, {}, {}, [GOOD_TEST]),
    ("ai-test-rejected", "statskit", {"body": PROSE}, {}, {}, [WRONG_TEST, WRONG_TEST]),
    ("ai-test-second-try", "statskit", {"body": PROSE}, {}, {"bisect": False}, [WRONG_TEST, GOOD_TEST]),
    ("ai-untrusted", "statskit", {"association": "NONE", "body": PROSE}, {}, {}, [GOOD_TEST]),
    ("ai-not-needed", "statskit", {}, {}, {}, [GOOD_TEST]),
    ("ai-invented-assertion", "statskit", {"body": "mean of an empty list should be zero but it is not."}, {}, {},
     ["```python\nfrom statskit import spread\nassert spread([1, 5]) == 99\n```"]),
    ("ai-patch", "statskit", {"body": SPREAD_BODY}, {}, {}, [SPREAD_FIX]),
    ("ai-patch-rejected", "statskit", {"body": SPREAD_BODY}, {}, {},
     [SPREAD_FIX.replace("return max(values) - min(values)\n=======", "return something_else\n======="),
      SPREAD_FIX.replace("    if not values:\n        return 0.0\n", "    pass\n")]),
    ("template-and-command", "statskit", {"body": "mean of nothing fails"}, {"test": "python3 -B {test_file}"},
     {"test_cmd": "python3 -c 'import statskit; statskit.mean([])'", "bisect": False}, None),
    ("command-fails-outside-project", "statskit", {"body": "it exits 1"}, {}, {"test_cmd": "python3 -c 'raise SystemExit(1)'", "bisect": False}, None),
    ("ai-not-asked-for-shell-steps", "statskit", {"body": "```sh\n$ python3 -c 'import statskit; statskit.nope()'\n```\n\nZeroDivisionError: division by zero"}, {}, {}, [GOOD_TEST]),
    ("ai-after-snippet-fails-differently", "statskit", {"body": "```python\nimport statskit\nstatskit.nope()\n```\n\nZeroDivisionError: division by zero"}, {}, {}, [GOOD_TEST]),
]


# Shapes the recorded issues turned out not to contain (found by planting bugs the check then missed).
HANDMADE = [
    '### Steps to reproduce\n\n1. run `make thing`\n2. then execute `python app.py --flag 3`\n\n```sh\n$ pytest -k foo\nValueError: boom\n```\n\n```python\nimport os\nprint(os.sep)\n```',
    '~~~python\nx = 1\n```\nprint(x)\n~~~\n\n```\n~~~\nValueError: boom\n```\n',
    "````\n```\nKeyError: 'inner'\n```\n````\n\n```python\nraise KeyError('inner')\n````\nafter\n",
    '1. open the app\n2. call `run()`\n\n### Environment\n\n| Key | Value |\n|---|---|\n| OS | macOS 14 |\n| python | 3.11.2 |\n\n### Python version\n\n3.12.1\n\n### Node\n\n_No response_\n',
    '```\nIn [1]: import statskit\n\nIn [2]: statskit.mean([])\n   ...: \nOut[2]: 0\nZeroDivisionError: division by zero\n```\n\n```\n>>> \n>>>\nValueError: x\n```\n\n```pycon\n>>> mean([])\n... \nZeroDivisionError: division by zero\n```',
    "```python\nfrom os.path import *\nprint(join('a', undefined_name))\n```\n\nNameError: name 'undefined_name' is not defined",
    "```python\nimport pytest\n\nclass TestThing:\n    def test_a(self):\n        assert mean([]) == 0\n\n@pytest.mark.parametrize('n', [1])\ndef test_b(n):\n    assert n\n\nasync def test_c():\n    pass\n```\n\n```\nFAILED t.py::TestThing::test_a - assert 1 == 0\n```",
    "```python\ndef test_a():\n    assert Path('x').exists()\n\ndef test_b():\n    assert datetime.now() and os.getcwd() and Flask(__name__) and werkzeug.routing.Map()\n```\n\nAssertionError",
    "```python\nmatch command:\n    case {'a': x, **rest}:\n        print(x, rest, y)\n    case [first, *others] as whole:\n        print(first, others, whole)\ntry:\n    pass\nexcept ValueError as err:\n    del err\nprint([i for i in range(3)], lambda q: q + z)\n```\n\nNameError: name 'y' is not defined",
    "```js\nimport express from 'express'\nconst app = express()\n```\n\n```javascript\nconst x = require('express')\nimport y from 'y'\n```\n\nTypeError: app.foo is not a function\n    at Object.<anonymous> (/srv/app/index.js:3:5)",
    '```console\n$ sudo apt install thing\n$ cd project\n$ pip install -e .\n```\n\nIt worked in 1.2.3 and broke in 1.3.0.\n\nerror: something failed',
    '```python\nimport sys\nprint(sys.version)\n```\n\n```python\n    import statskit\n    statskit.mean([])\n```\n\n```python\nthis is not python\n```\n\n```\nrepro.py:3: error: Argument 1 has incompatible type  [arg-type]\n```\npyright',
    '```\nIn [1]: mean([])\n>>> spread([])\nZeroDivisionError: division by zero\n```',
    '```python\ndef test_a():\n    assert mean([]) == 0\n\ndef main():\n    return 1\n\nmain()\n```\n\nZeroDivisionError: division by zero',
    "```js\nconst x = require('express')\nimport y from 'y'\nx()\n```\n\nTypeError: x is not a function",
    "```python\nimport werkzeug as w\nimport flask\nimport statskit\nflask.json.dumps({})\nwerkzeug.routing.Map()\nstatskit.core.mean([])\n```\n\nNameError: name 'werkzeug' is not defined",
    'panic: runtime error: index out of range [3] with length 3\n\ngoroutine 1 [running]:\nmain.main()\n\t/home/u/app/main.go:12 +0x1d\n\nGo version: go1.22.1\nrustc 1.77.0\nWindows Server 2022',
    "```python\nimport werkzeug\nimport flask as f\nwerkzeug.routing.Map()\nwerkzeug.nothing.here()\nflask.json.dumps({})\n```\n\n```sh\npython3 repro.py --port 8080\n```\n\nAttributeError: module 'werkzeug' has no attribute 'routing'",
]


def digest(value) -> str:
    return hashlib.sha1(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()[:16]


def safe(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception as exc:  # both sides must fail the same way
        return ["!raised", type(exc).__name__]


def scrub(text: str, roots=()) -> str:
    for root in roots:
        text = text.replace(str(root), "ROOT")
    for pattern, repl in VOLATILE:
        text = pattern.sub(repl, text)
    return text


# --- inputs ---------------------------------------------------------------------------------

def damaged(raw: dict, rng: random.Random) -> list[dict]:
    """Copies of an issue with the kinds of damage reporters do to Markdown."""
    body = raw.get("body") or ""
    variants = {
        "nolang": re.sub(r"^(\s*(?:```|~~~))[\w+#.-]+\s*$", r"\1", body, flags=re.M),
        "indented": "\n".join("    " + l if not l.startswith(("```", "#", "~~~")) else l for l in body.split("\n")),
        "cut": body[: rng.randrange(len(body) + 1)],
        "bold": re.sub(r"^#{1,6}\s+(.+?)\s*$", r"**\1**", body, flags=re.M),
        "crlf": body.replace("\n", "\r\n"),
        "untitled": body,
    }
    out = []
    for name in rng.sample(sorted(variants), 2):
        copy = dict(raw, body=variants[name])
        if name == "untitled":
            copy.update(title=None, user=None, labels=["bug", {"name": "x"}], author="someone", number=None)
        copy["_id"] = f"{raw['_id']}~{name}"
        out.append(copy)
    return out


def load_issues(path: str | None) -> list[dict]:
    sys.path.insert(0, str(Path(os.environ["SCOUT_TESTS"]).parent))
    from tests import fixture

    issues = [dict(fixture.issue(), _id="fixture")]
    issues += [dict(fixture.issue(number=100 + i, **fields), _id=f"scenario/{name}") for i, (name, _r, fields, *_rest) in enumerate(SCENARIOS)]
    issues += [dict(fixture.issue(number=900 + i, body=body), _id=f"handmade/{i}") for i, body in enumerate(HANDMADE)]
    if path:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            for repo, found in sorted(json.load(fh).items()):
                issues += [dict(raw, _id=f"{repo}#{raw['number']}") for raw in found]
    rng = random.Random(7)
    return issues + [copy for raw in issues for copy in damaged(raw, rng)]


# --- pure functions -------------------------------------------------------------------------

def snap_text_functions(issues: list[dict], trees: list[Path], snap: dict) -> None:
    from bounty_scout import issue_parser, repro
    from bounty_scout.runner import SETUP_FAILED, RunResult

    indexes = [repro.project_index(tree) for tree in trees]
    signatures, logs, blocks = [], [], []
    for i, raw in enumerate(issues):
        key = raw["_id"]
        clean = {k: v for k, v in raw.items() if k != "_id"}
        parsed = safe(issue_parser.parse_issue, clean)
        if isinstance(parsed, list):
            snap[f"parse/{key}"] = digest(parsed)
            continue
        snap[f"parse/{key}"] = digest(parsed.to_dict())
        signatures.append(parsed.error_signature)
        logs.append("\n".join(parsed.error_logs) or (clean.get("body") or ""))
        blocks += [(f"{key}/{n}", b.text) for n, b in enumerate(parsed.code_blocks) if b.kind == "python"]
        for label, kwargs in (("plain", {}), ("cmd", {"test_cmd": "pytest -x -k thing"}), ("match", {"match": "boom 42"})):
            plan = safe(repro.build_plan, issue_parser.parse_issue(clean), **kwargs)
            if dataclasses.is_dataclass(plan):
                tree = trees[(i // 3) % len(trees)] if trees else None
                if tree and label == "plain" and i % 3 == 0:  # indexing a project is the slow step
                    done = safe(repro.complete_imports, plan, tree)
                    snap[f"imports/{key}"] = digest([done, dataclasses.asdict(plan)])
                if label == "plain" and key.startswith("handmade/"):  # the handmade ones against every tree
                    for n, other in enumerate(trees):
                        copy = dataclasses.replace(plan, notes=list(plan.notes))
                        snap[f"imports/{key}/{n}"] = digest([safe(repro.complete_imports, copy, other), dataclasses.asdict(copy)])
                plan = dataclasses.asdict(plan)
            snap[f"plan/{label}/{key}"] = digest(plan)
        snap[f"parse-after-plan/{key}"] = digest(parsed.to_dict())

    for key, text in blocks:
        snap[f"blocks/{key}"] = digest([
            safe(repro._pytest_style, text, False), safe(repro._pytest_style, text, True), safe(repro.missing_names, text),
            [safe(repro.resolve_imports, safe(repro.missing_names, text), top, exports, text) for top, exports in indexes],
            [safe(repro.resolve_imports, ["Path", "os", "datetime", "Flask", "mean", "nothing_here"], top, exports) for top, exports in indexes],
        ])

    known = [(i, s) for i, s in enumerate(signatures) if s]
    for n, (i, signature) in enumerate(known):
        pattern = repro.signature_pattern(signature)
        row = [pattern]
        for j in (i, known[(n + 1) % len(known)][0], known[(n + 5) % len(known)][0]):
            for strict in (False, True):
                row.append(repro.match_kind(signature, pattern, logs[j], strict))
            row.append(repro.match_kind(signature, None, logs[j]))
            for exit_code, timed_out, extra in ((0, False, ""), (1, False, ""), (125, False, SETUP_FAILED), (1, True, "")):
                result = RunResult(exit_code, logs[j] + extra, timed_out, 0.0)
                row.append(repro.classify(result, pattern, signature, False))
                row.append(repro.classify(result, None, None))
        snap[f"match/{n}"] = digest(row)
    snap["match/corners"] = [[repro.match_kind(sig, repro.signature_pattern(sig), out, strict), repro.match_kind(sig, None, out, strict)]
                             for sig, out in MATCH_CORNERS for strict in (False, True)]


# Reported errors and outputs for the corners of match_kind() that no recorded issue reaches.
MATCH_CORNERS = [
    ("Token.Name.Exception", "x\nToken.Name.Exception\n"), ("ValueError", "ValueError: boom"), ("pkg.CustomError", "E   pkg.CustomError\n"),
    ("Error: one two", "Error: two one"), ("ValueError: alpha beta gamma delta epsilon zeta eta", "ValueError: eta zeta epsilon delta gamma beta alpha"),
    ("AssertionError: assert 1 == 2", "AssertionError\n"), ("AssertionError: boom", "AssertionError\n"), ("KeyError: 'a'", "KeyError: 'b'"),
    ("KeyError: 'a'", "\x1b[31mKeyError\x1b[0m: 'a' extra"), ("os.error: [Errno 2] No such file: '/tmp/x'", "FileNotFoundError: [Errno 2] No such file: '/home/y'"),
]


def snap_repositories(issues: list[dict], repos: list[Path], layout: Path, snap: dict) -> None:
    from bounty_scout import issue_parser, localize, repro
    from bounty_scout.runner import git

    for limit in (1500, 9, 4, 1, 0):
        top, exports = repro.project_index(layout, limit)
        snap[f"index/layout/{limit}"] = [sorted(top), sorted(exports.items())]
    names = ["alias", "other", "thing", "single", "pkg", "Kind", "VALUE", "J", "sep", "os", "DEEP", "datetime", "Path", "nothing"]
    index = repro.project_index(layout)
    snap["imports/layout"] = [repro.resolve_imports(names, *index, code) for code in ("", "pkg.sub.deep.thing()\npkg.a.x\ndatetime(1, 1, 1)\nos.sep")]

    reported = []
    for raw in issues[:4000]:
        parsed = safe(issue_parser.parse_issue, {k: v for k, v in raw.items() if k != "_id"})
        if not isinstance(parsed, list):
            reported += parsed.stack_frames
    reported = list(dict.fromkeys(reported))
    for repo in repos:
        name = repo.name
        for limit in (1500, 7):
            top, exports = repro.project_index(repo, limit)
            snap[f"index/{name}/{limit}"] = digest([sorted(top), sorted(exports.items())])
        tracked = git(repo, "ls-tree", "-r", "--name-only", "HEAD").stdout.splitlines()
        rng = random.Random(11)
        made = []
        for path in rng.sample(tracked, min(len(tracked), 400)):
            base = path.rsplit("/", 1)[-1]
            made += [(path, 3), (base, 4), ("/work/" + path, 5), ("./" + path, 6), ("/usr/lib/python3/site-packages/" + path, 7),
                     ("/".join(path.split("/")[-2:]), 8), ("elsewhere/" + base, 9), ("", 1), ("./", 2)]
        for prefix in (None, "/work"):
            snap[f"frames/{name}/{prefix}"] = digest(localize.resolve_frames(repo, "HEAD", reported + made, prefix))
        commits = git(repo, "rev-list", "--no-merges", "--max-count=60", "HEAD").stdout.split()
        for sha in commits:
            hunks = safe(localize.changed_hunks, repo, sha)
            if not hunks or isinstance(hunks[0], str):
                snap[f"locate/{name}/{sha[:10]}"] = digest(safe(localize.locate, repo, sha, "HEAD", []))
                continue
            on = [(h.file, h.start) for h in hunks[:6]]
            near = [(h.file, h.start + h.count + 2) for h in hunks[:6]]
            far = [(h.file, h.start + 500) for h in hunks[:3]]
            other = [(rng.choice(tracked), 1)]
            found = [safe(localize.locate, repo, sha, "HEAD", frames) for frames in ([], on, near, far, other, other + on, on + far)]
            snap[f"locate/{name}/{sha[:10]}"] = digest([dataclasses.asdict(f) if dataclasses.is_dataclass(f) else f for f in found])


# --- the pipeline ---------------------------------------------------------------------------

def model_config(box: Path, replies) -> dict:
    box.mkdir(parents=True)
    (box / "stub.py").write_text(STUB)
    for i, reply in enumerate(replies, 1):
        (box / f"reply-{i}.txt").write_text(reply)
    return {"ai": "command", "ai_command": f"{sys.executable} {box / 'stub.py'} {box}"}


def snap_triage(work: Path, snap: dict) -> None:
    from bounty_scout.config import Config
    from bounty_scout.report import confidence, render
    from bounty_scout.triage import Options, triage
    from tests import fixture

    for n, (name, repo, fields, cfg, opts, replies) in enumerate(SCENARIOS):
        cfg = dict(cfg)
        if replies is not None:
            cfg.update(model_config(work / "model" / name, replies))
        result = safe(triage, work / "repos" / repo, fixture.issue(number=100 + n, **fields), Config(**cfg), Options(**opts))
        if isinstance(result, list):
            snap[f"triage/{name}"] = result
            continue
        prompts = [p.read_text() for p in sorted((work / "model" / name).glob("prompt-*.txt"))] if replies is not None else []
        text = json.dumps([dataclasses.asdict(result), render(result), confidence(result), prompts], default=str, sort_keys=True)
        snap[f"triage/{name}"] = scrub(text, [work])


def snap_cli(work: Path, snap: dict) -> None:
    from bounty_scout import cli, github
    from tests import fixture

    repo = work / "repos" / "statskit"
    files = work / "cli"
    files.mkdir()
    (files / "one.json").write_text(json.dumps(fixture.issue()))
    (files / "many.json").write_text(json.dumps([fixture.issue(), fixture.issue(number=43, body="It crashes sometimes."),
                                                 fixture.issue(number=44, association="NONE")]))
    (files / "none.json").write_text("[]")
    posted = []
    github._gh = lambda *args, stdin=None: posted.append([list(args), stdin]) or "https://example.invalid/comment\n"
    runs = {
        "one": ["--issue-file", "one.json"],
        "one-quiet-no-bisect": ["--issue-file", "one.json", "--quiet", "--no-bisect"],
        "many": ["--issue-file", "many.json"],
        "none": ["--issue-file", "none.json"],
        "bad-ref": ["--issue-file", "many.json", "--bad", "no-such-ref"],
        "publish": ["--issue-file", "many.json", "--publish", "--repo", "example/statskit", "--no-bisect"],
        "publish-no-repo": ["--issue-file", "one.json", "--publish"],
        "ai-command": ["--issue-file", "one.json", "--ai-cmd", "true", "--no-bisect"],
        "bad-ai": ["--issue-file", "one.json", "--ai", "command", "--no-bisect"],
        "options": ["--issue-file", "one.json", "--good", "v1.0.0", "--match", "ZeroDivisionError", "--timeout", "30", "--first-parent",
                    "--trust-issue-code", "--setup-cmd", "true"],
    }
    for name, extra in runs.items():
        out_dir = files / f"out-{name}"
        argv = ["triage", "--repo-path", str(repo), "--out", str(out_dir)] + [str(files / a) if a.endswith(".json") else a for a in extra]
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                code = cli.main(argv)
            except SystemExit as exc:
                code = ["exit", str(exc.code)]
        written = {p.name: p.read_text() for p in sorted(out_dir.glob("*"))} if out_dir.exists() else None
        snap[f"cli/{name}"] = scrub(json.dumps([code, stdout.getvalue(), stderr.getvalue(), written, posted], sort_keys=True), [work])
        posted.clear()
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
        try:
            outside = cli.main(["triage", "--repo-path", str(files), "--issue-file", str(files / "one.json")])
        except SystemExit as exc:
            outside = ["exit", str(exc.code)]
    snap["cli/not-a-repository"] = scrub(json.dumps([outside, stdout.getvalue()]), [work])


# --- made-up results: the report and the field test -----------------------------------------

def made_up_result(rng: random.Random, raw: dict, bad: str = "", golden: str | None = None):
    """A triage result with every optional part present or absent at random."""
    from bounty_scout import bisect as bisect_mod
    from bounty_scout.issue_parser import parse_issue
    from bounty_scout.localize import Fix, Suspect
    from bounty_scout.repro import ReproPlan, Verdict
    from bounty_scout.runner import RunResult
    from bounty_scout.triage import TriageResult

    pick, maybe = rng.choice, lambda p=0.5: rng.random() < p
    sha = lambda: "".join(pick("0123456789abcdef") for _ in range(40))
    commit = lambda: bisect_mod.Commit(sha(), pick(["Simplify mean() (#12)", "tidy | pipes @someone", "x"]), pick(["Ana", "b@c"]), "2024-01-02")
    status = pick(["reproduced"] * 6 + ["not-reproduced", "inconclusive", "needs-info", "needs-approval", "unsupported"])
    r = TriageResult(parse_issue({k: v for k, v in raw.items() if not k.startswith("_")}), status,
                     bad_ref=pick(["HEAD", "v2.0"]), bad_sha=bad if bad else (sha() if maybe(0.8) else ""), sandbox=pick(["worktree", "docker"]))
    if status in ("reproduced", "not-reproduced", "inconclusive") or maybe(0.2):
        reason = pick(["reported error present in output", "same error type with a similar message in output",
                       "same error type in output; the report gave no message to compare", "exited 1", "exited 0", "timed out"])
        output = pick(["", "Traceback\n  File x\nValueError: boom\n", "\n".join(f"line {i}" for i in range(60)), "a ``` fence\n| pipe"])
        r.head = Verdict(pick(["bad", "good", "skip"]), reason, RunResult(pick([0, 1, 2]), output, maybe(0.1), rng.random() * 9))
    source = pick(["python-snippet", "node-snippet", "shell-steps", "test-cmd", "ai-test", None])
    if source:
        named = source != "test-cmd"
        r.plan = ReproPlan(source, pick(["python3 {test_file}", "node {test_file}", "sh {test_file}", "pytest -x"]),
                           pick(["test_issue_7.py", "test_issue_7.mjs", "repro_issue_7.sh", "odd"]) if named else None,
                           pick(["print(1)\n", "x = '```'\n", ""]) if named else None, pick([None, "ValueError: boom"]))
    r.command = pick(["", "python3 /scout/test_issue_7.py"])
    r.good_ref = pick([None, "v1.1.0", sha()])
    r.probes = [(pick(["v1.0", sha()]), pick(["good", "bad", "skip"]), "exited 1") for _ in range(rng.randrange(3))]
    kind = pick([None, None, "found", "found", "ambiguous", "abandoned", "failed"])
    if kind:
        steps = [{"sha": sha(), "verdict": pick(["good", "bad", "skip"]), "reason": "exited 1", "seconds": 1.5} for _ in range(rng.randrange(4))]
        culprit = bisect_mod.Commit(golden, "the known culprit", "Ana", "2024-01-02") if golden and maybe(0.7) else commit()
        r.bisect = bisect_mod.BisectResult(kind, "v1.1.0", "HEAD", culprit if kind == "found" else None,
                                           [commit() for _ in range(rng.randrange(1, 4))] if kind == "ambiguous" else [],
                                           steps, rng.randrange(1, 90), "", pick(["", "", "3 commits could not be tested | @x", "git said no\nnewline"]))
    if maybe():
        r.suspect = Suspect(pick(["src/werkzeug/routing.py", "pkg/core.py"]), pick([None, 7]), pick(["high", "medium", "low"]), "a stack frame lands there")
    if maybe():
        r.fix = Fix("--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n", pick(["hunk", "file", "commit", "ai"]),
                    pick(["verified", "still-fails", "does-not-apply", "unverified"]))
    r.notes = [pick(["Ran on Python 3.9, to match the version in the report.", "A note.", "Another | note"]) for _ in range(rng.randrange(3))]
    r.match = pick([None, "exact", "similar", "type-only", "exit-code"])
    r.in_project = maybe(0.7)
    r.regression = pick([None, "yes", "no", "unknown"])
    r.oldest_failing = pick([None, "v0.1"])
    r.ai_label = pick([None, None, "custom command", "anthropic/some-model"])
    r.ai_calls = rng.randrange(4)
    r.ai_test_gate = pick([None, None, "reported-error", "assertion"])
    return r


def snap_reports(issues: list[dict], snap: dict) -> None:
    from bounty_scout.report import confidence, render

    rng = random.Random(3)
    for n in range(6000):
        result = made_up_result(rng, issues[n % len(issues)])
        snap[f"report/{n}"] = digest([safe(render, result), safe(confidence, result)])


def snap_field_test(work: Path, fetch_tape: str | None, repos: list[Path], snap: dict) -> None:
    import field_test

    golden = json.loads((Path(field_test.__file__).parent / "golden.json").read_text())["culprits"]
    fetched = {}
    if fetch_tape:
        with gzip.open(fetch_tape, "rt", encoding="utf-8") as fh:
            tape = json.load(fh)
        field_test.gh = lambda path: tape[path]
        for n, (repo, kind, prose) in enumerate([("pallets/werkzeug", "python", False), ("expressjs/express", "node", False),
                                                 ("pallets/click", "python", True), ("pallets/werkzeug", "python", True)]):
            for issues in (120, 15):
                out = work / f"fetch-{n}-{issues}.json"
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    code = safe(field_test.cmd_fetch, argparse.Namespace(repo=repo, output=str(out), pages=4, issues=issues, kind=kind, prose=prose))
                text = out.read_text() if out.exists() else None
                snap[f"field/fetch/{repo}/{kind}/{prose}/{issues}"] = digest([code, stderr.getvalue(), text])
                if text and issues == 120:
                    fetched.setdefault(repo, json.loads(text))

    field_test.time.monotonic = lambda: 0.0
    by_name = {repo.name: repo for repo in repos}
    for slug, data in sorted(fetched.items()):
        repo = by_name.get(slug.split("/")[1])
        if repo is None or not data["candidates"]:
            continue
        known = golden.get(slug, {})

        def fake_triage(repo_path, raw, cfg, options, known=known):
            if raw["number"] % 13 == 0:
                raise RuntimeError("a made-up failure")
            rng = random.Random(f"{raw['number']}/{options.bad}/{options.bisect}")
            return made_up_result(rng, raw, options.bad, known.get(str(raw["number"])))

        field_test.triage = fake_triage
        issues_file = work / f"candidates-{repo.name}.json"
        issues_file.write_text(json.dumps(data))
        for limit, ai in ((40, "off"), (5, "off"), (1000, "anthropic")):
            out = work / f"run-{repo.name}-{limit}-{ai}"
            stdout = io.StringIO()
            args = argparse.Namespace(repo_path=str(repo), issues=str(issues_file), out=str(out), image="python:{python}-slim", limit=limit,
                                      timeout=60, sandbox="docker", set="main", ai=ai, ai_model=None)
            with contextlib.redirect_stdout(stdout):
                code = safe(field_test.cmd_run, args)
            written = {str(p.relative_to(out)): p.read_text() for p in sorted(out.rglob("*")) if p.is_file()}
            snap[f"field/run/{slug}/{limit}/{ai}"] = digest([code, stdout.getvalue(), written])

    rng = random.Random(5)
    for n in range(300):
        rows = []
        for _ in range(rng.randrange(12)):
            row = {"issue": rng.randrange(900)}
            for key, values in (("status", ["reproduced", "not-reproduced", "inconclusive", "error", "unsupported", None]),
                                ("fix_commit", [None, "abc"]), ("fix_changes_code", [None, True, False]),
                                ("after_fix", [None, "reproduced", "not-reproduced", "inconclusive"]), ("in_project", [None, True, False]),
                                ("match", [None, "exact", "similar", "type-only"]), ("confidence", [None, "high", "medium", "low"]),
                                ("regression", [None, "yes", "no", "unknown"]), ("bisect", [None, "found", "ambiguous", "abandoned", "failed"]),
                                ("culprit_in_thread", [None, True, False]), ("suspect_in_fix", [None, True, False]),
                                ("patch", [None, "verified", "still-fails"]), ("patch_scope", [None, "hunk", "ai", "file"]),
                                ("test_source", [None, "ai-test", "python-snippet"]), ("ai_calls", [None, 0, 2]),
                                ("culprit", [None, "0123456789abcdef"]), ("suspect", [None, "a.py:3"])):
                value = rng.choice(values)
                if value is not None or rng.random() < 0.3:
                    row[key] = value
            rows.append(row)
        summary = safe(field_test.summarise, {"scanned": n}, rows)
        snap[f"field/summarise/{n}"] = digest([summary, safe(field_test.markdown, "o/r", summary, rows) if isinstance(summary, dict) else None])


# --- running both sides ---------------------------------------------------------------------

def take_snapshot(args) -> int:
    work = Path(args.work)
    issues = load_issues(args.issues)
    repos = [Path(p) for p in args.repo]
    snap: dict = {}
    parts = {
        "text": lambda: snap_text_functions(issues, [work / "repos" / "statskit"] + repos[:2], snap),
        "repositories": lambda: snap_repositories(issues, repos + [work / "repos" / "statskit"], work / "repos" / "layout", snap),
        "reports": lambda: snap_reports(issues, snap),
        "field": lambda: snap_field_test(work, args.fetch, repos, snap),
        "triage": lambda: snap_triage(work, snap),
        "cli": lambda: snap_cli(work, snap),
    }
    wanted = args.only.split(",") if args.only else [name for name in parts if not (args.quick and name in ("triage", "cli"))]
    for name in wanted:
        parts[name]()
    Path(args.snapshot).write_text(json.dumps(snap, sort_keys=True, indent=0))
    return 0


def build_repositories(dest: Path) -> None:
    """The small repositories the pipeline runs on. Built once, then copied, so both sides see the same commits."""
    sys.path.insert(0, str(ROOT))
    from tests import fixture

    fixture.build(dest / "statskit")
    info = fixture.build(dest / "threeway", commits_after=2)
    core = info["repo"] / "statskit" / "core.py"
    fixture._commit(info["repo"], "Comment spread()", {"statskit/core.py": core.read_text().replace(
        "return max(values) - min(values)", "return max(values) - min(values)  # range")})
    for name, text in LAYOUT.items():  # every case project_index() distinguishes, in one tree (not a repository)
        (dest / "layout" / name).parent.mkdir(parents=True, exist_ok=True)
        (dest / "layout" / name).write_text(text)
    repo = dest / "unknown"
    fixture._git(dest, "init", "-q", "-b", "main", str(repo))
    fixture._commit(repo, "one", {"pkg/__init__.py": ""})
    fixture._commit(repo, "two", {"pkg/tool.py": "def run():\n    raise KeyError('boom')\n"})
    repo = dest / "hunk"
    filler = "".join(f"# line {i}\n" for i in range(30))
    good = "def mean(values):\n    if not values:\n        return 0.0\n    return sum(values) / len(values)\n\n" + filler + "LIMIT = 10\n"
    fixture._git(dest, "init", "-q", "-b", "main", str(repo))
    fixture._commit(repo, "start", {"pkg/__init__.py": "from .core import mean\n", "pkg/core.py": good})
    fixture._git(repo, "tag", "v1")
    broken = good.replace("    if not values:\n        return 0.0\n", "").replace("LIMIT = 10", "LIMIT = 20")
    fixture._commit(repo, "tidy", {"pkg/core.py": broken})
    fixture._commit(repo, "raise limit", {"pkg/core.py": broken.replace("LIMIT = 20", "LIMIT = 30")})


LAYOUT = {
    "src/pkg/__init__.py": "from .a import thing as alias, other\nfrom .b import *\nfrom . import a\n__all__ = ['alias']\n",
    "src/pkg/a.py": "def thing(): pass\nasync def co(): pass\nclass Kind: pass\nx: int = 1\nA = B = 2\n_private = 1\nobj.attr = 3\ny: int\nimport os\n",
    "src/pkg/b.py": "from os.path import join as J, sep\nVALUE = 1\n",
    "src/pkg/broken.py": "def (:\n",
    "src/pkg/sub/__init__.py": "",
    "src/pkg/sub/deep.py": "def thing(): pass\nDEEP = 1\n",
    "src/pkg/tests/test_a.py": "HIDDEN_IN_TESTS = 1\n",
    "src/docs/__init__.py": "IN_DOCS = 1\n",
    "src/notpkg/mod.py": "NOT_A_PACKAGE = 1\n",
    "src/_hidden.py": "HIDDEN = 1\n",
    "single.py": "def single(): pass\nVALUE = 2\n",
    "setup.py": "SETUP = 1\n",
    "conftest.py": "CONF = 1\n",
    ".dot/__init__.py": "DOT = 1\n",
    "other/__init__.py": "from pkg import alias\nthing = 1\n",
    "other/examples/ex.py": "EXAMPLE = 1\n",
    "README.md": "# layout\n",
}


def compare(args) -> int:
    with tempfile.TemporaryDirectory(prefix="behaviour-") as tmp:
        tmp = Path(tmp)
        old_tree = tmp / "old-tree"
        old_tree.mkdir()
        archive = subprocess.run(["git", "-C", str(ROOT), "archive", args.base, "src", "fieldtest", "tests"], check=True, capture_output=True).stdout
        subprocess.run(["tar", "-x", "-C", str(old_tree)], input=archive, check=True)
        build_repositories(tmp / "repos")
        running = {}
        for side, tree in (("old", old_tree), ("new", ROOT)):
            work = tmp / side
            work.mkdir()
            shutil.copytree(tmp / "repos", work / "repos", symlinks=True)
            env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(tree / "src"), str(tree / "fieldtest"), str(tree)]),
                       SCOUT_TESTS=str(tree / "tests"), PYTHONHASHSEED="0", PYTHONDONTWRITEBYTECODE="1")
            argv = [sys.executable, "-W", "ignore", __file__, "--snapshot", str(tmp / f"{side}.json"), "--work", str(work)]
            argv += (["--issues", args.issues] if args.issues else []) + (["--fetch", args.fetch] if args.fetch else [])
            argv += [x for repo in args.repo for x in ("--repo", repo)] + (["--quick"] if args.quick else []) + (["--only", args.only] if args.only else [])
            running[side] = subprocess.Popen(argv, env=env)
        if any(proc.wait() for proc in running.values()):
            print("a snapshot run failed", file=sys.stderr)
            return 2
        old, new = (json.loads((tmp / f"{side}.json").read_text()) for side in ("old", "new"))
        if args.keep:
            shutil.copy(tmp / "old.json", args.keep + ".old.json")
            shutil.copy(tmp / "new.json", args.keep + ".new.json")
    differing = sorted(k for k in old.keys() | new.keys() if old.get(k) != new.get(k))
    sections: dict[str, int] = {}
    for key in old:
        sections[key.split("/")[0]] = sections.get(key.split("/")[0], 0) + 1
    print(f"{len(old)} outputs compared: " + ", ".join(f"{name} {count}" for name, count in sorted(sections.items())))
    if not differing:
        print("identical")
        return 0
    print(f"{len(differing)} differ:")
    for key in differing[:40]:
        print("  " + key)
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("base", nargs="?", help="git ref to compare the working tree with")
    parser.add_argument("--issues", help="issues recorded by scripts/record_github.py corpus")
    parser.add_argument("--fetch", help="answers recorded by scripts/record_github.py fetch")
    parser.add_argument("--repo", action="append", default=[], help="a real repository to index and localise in (repeatable)")
    parser.add_argument("--quick", action="store_true")
    parser.add_argument("--only", help="comma-separated parts to run: text, repositories, reports, field, triage, cli")
    parser.add_argument("--keep", help="save both snapshots as <prefix>.old.json and <prefix>.new.json")
    parser.add_argument("--snapshot", help=argparse.SUPPRESS)
    parser.add_argument("--work", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.snapshot:
        return take_snapshot(args)
    if not args.base:
        parser.error("give the ref to compare with")
    return compare(args)


if __name__ == "__main__":
    sys.exit(main())
