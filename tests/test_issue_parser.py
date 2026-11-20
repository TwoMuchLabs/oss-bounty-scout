import unittest

from bounty_scout.issue_parser import extract_signature, parse_issue

from . import fixture


class ParserTests(unittest.TestCase):
    def setUp(self):
        self.issue = parse_issue(fixture.issue())

    def test_metadata(self):
        self.assertEqual(self.issue.number, 42)
        self.assertEqual(self.issue.author, "reporter")
        self.assertEqual(self.issue.author_association, "MEMBER")
        self.assertEqual(self.issue.labels, ["bug"])

    def test_steps_and_code(self):
        self.assertEqual(self.issue.steps, ["Install statskit from main", "Run the snippet below"])
        kinds = [(b.kind, b.section) for b in self.issue.code_blocks]
        self.assertEqual(kinds, [("python", "repro"), ("log", "logs")])
        self.assertIn("print(mean([]))", self.issue.code_blocks[0].text)

    def test_error_signature_and_frames(self):
        self.assertEqual(self.issue.error_signature, "ZeroDivisionError: division by zero")
        self.assertEqual(self.issue.stack_frames[-1],
                         ("/home/dev/.venv/lib/python3.12/site-packages/statskit/core.py", 7))

    def test_environment(self):
        self.assertEqual(self.issue.environment["os"], "Ubuntu 22.04")
        self.assertEqual(self.issue.environment["python"], "3.12.1")
        self.assertEqual(self.issue.environment["statskit"], "main")

    def test_version_hints(self):
        self.assertEqual(self.issue.good_hints, ["1.1.0"])
        self.assertEqual(self.issue.missing, [])

    def test_issue_form_and_console_block(self):
        body = (
            "### Version\n\n2.4.1\n\n### Operating system\n\nmacOS 14.5\n\n"
            "### What happened?\n\nBroke in v2.4.0, last working version was 2.3.9.\n\n"
            "### Relevant log output\n\n```console\n$ pip install mytool\n$ mytool build --fast\n"
            "error: unknown flag '--fast'\n```\n"
        )
        issue = parse_issue({"number": 7, "title": "build --fast fails", "body": body})
        self.assertEqual(issue.environment["version"], "2.4.1")
        self.assertEqual(issue.environment["operating system"], "macOS 14.5")
        self.assertEqual(issue.shell_commands, ["pip install mytool", "mytool build --fast"])
        self.assertEqual(issue.good_hints, ["2.3.9"])
        self.assertEqual(issue.bad_hints, ["v2.4.0"])
        self.assertEqual(issue.error_signature, "error: unknown flag '--fast'")

    def test_repl_session_is_split_into_code_and_output(self):
        body = "```\n>>> import json\n>>> json.loads('{')\nTraceback (most recent call last):\n  ...\njson.decoder.JSONDecodeError: Expecting property name\n```"
        issue = parse_issue({"number": 1, "title": "t", "body": body})
        self.assertEqual(issue.code_blocks[0].kind, "python")
        self.assertEqual(issue.code_blocks[0].text, "import json\njson.loads('{')")
        self.assertEqual(issue.error_signature, "json.decoder.JSONDecodeError: Expecting property name")

    def test_empty_issue_reports_what_is_missing(self):
        issue = parse_issue({"number": 3, "title": "it is broken", "body": "Please fix."})
        self.assertEqual(len(issue.missing), 3)
        self.assertIsNone(issue.error_signature)

    def test_null_body(self):
        self.assertEqual(parse_issue({"number": 4, "title": "x", "body": None}).code_blocks, [])

    def test_signature_prefers_root_cause(self):
        log = "ValueError: outer\n\nDuring handling of the above exception:\n\nKeyError: 'inner'"
        self.assertEqual(extract_signature([log]), "KeyError: 'inner'")
        self.assertEqual(extract_signature(["panic: runtime error: index out of range [3]"]),
                         "panic: runtime error: index out of range [3]")


if __name__ == "__main__":
    unittest.main()
