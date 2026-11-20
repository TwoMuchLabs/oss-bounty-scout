# oss-bounty-scout

A background triage agent for GitHub maintainers. For each newly opened issue it:

1. **Parses** the issue: reproduction steps, runnable code, error logs, stack frames, environment, version hints.
2. **Reproduces** it in a temporary git worktree (or a network-less container) with a generated failing test.
3. **Bisects** with `git bisect` to the commit that introduced the regression.
4. **Reports** in Markdown: reproduction status, culprit commit, suspected file and line, and a candidate fix diff that has been re-tested against the reproduction.

Nothing is posted to GitHub unless you pass `--publish`.

No LLM, no API keys, no dependencies beyond Python 3.11+, `git` and the [GitHub CLI](https://cli.github.com). Everything is deterministic and runs on your machine or your CI runner.

## Install

```sh
pip install git+https://github.com/TwoMuchLabs/oss-bounty-scout
```

## Use

Run from a full (non-shallow) clone of the repository you maintain:

```sh
oss-bounty-scout triage --issue 123            # one issue; report printed and saved to scout-reports/
oss-bounty-scout triage --since 24h            # every issue opened in the last 24 hours
oss-bounty-scout triage --issue 123 --publish  # also comment the report on the issue
```

Other commands: `fetch` (issues as JSON), `parse` (show what was extracted), `publish` (post a saved report). `--issue-file` reads issue JSON from a file so triage can run without a token.

Useful `triage` options:

| Option | Purpose |
|---|---|
| `--test-cmd CMD` | Use your own reproduction command instead of the issue's code (exit 0 = good). |
| `--match TEXT` | Text that identifies the failure in the output. Default: the error reported in the issue. |
| `--good REF` / `--bad REF` | Bisect range. Default good: versions the reporter said worked, then recent tags, then the root commit. Default bad: `HEAD`. |
| `--setup-cmd CMD` | Run before the test at every commit (for example a build step). |
| `--sandbox docker` | Run in a container with networking disabled. |
| `--trust-issue-code` | Execute code from the issue even if its author is not a maintainer. |

Defaults can live in `.bounty-scout.toml` at the repository root:

```toml
setup = "make build"
test = "pytest -x {test_file}"   # how to run the generated test
sandbox = "docker"
docker_image = "ghcr.io/you/project-ci:latest"   # an image that already has your dependencies
timeout = 180
```

## How a commit is judged

The same check is used to confirm the bug, find a good baseline, drive `git bisect run`, and verify the fix:

- **bad**: the reported error appears in the output (or, with no known error, the command exits non-zero)
- **good**: the command exits 0 without it
- **skip**: setup failed, the run timed out, or it failed with a *different* error

Report statuses: `reproduced`, `not-reproduced`, `inconclusive` (failed differently), `needs-info` (nothing runnable in the issue), `needs-approval` (see below).

## Safety

Issues contain code written by strangers. By default, code from an issue is executed only when its author is an `OWNER`, `MEMBER` or `COLLABORATOR`; otherwise the report shows the generated test and stops at `needs-approval`.

- The worktree runner protects your checkout and scrubs the environment (no tokens, throw-away `HOME`). It is **not** a security sandbox.
- For untrusted code use `--sandbox docker`: no network, dropped capabilities, CPU/memory/PID limits, non-root user.
- Your working tree, index and any bisect you have in progress are never touched.

## GitHub Action

Copy [`templates/bounty-scout.yml`](templates/bounty-scout.yml) to `.github/workflows/` in your repository. It runs when a maintainer opens an issue or when the `scout` label is added, keeps the token out of the step that executes issue code, and writes the report to the job summary and an artifact. Set the repository variable `SCOUT_PUBLISH=true` to have it comment on the issue.

## Limits

- Reproduction needs a runnable Python or Node snippet, shell commands, or your `--test-cmd`. Bugs that show up as wrong output rather than an error need `--test-cmd`.
- The failing test is the reporter's snippet wrapped as a test; it is not minimised further.
- The suggested fix is a revert of the culprit's change (to the suspect file, else the whole commit). It restores old behaviour and is a starting point for a real fix.
- POSIX only. Docker mode has unit tests for the command it builds but has not yet been exercised against a live daemon.

## Development

```sh
pip install -e .
python -m unittest discover -s tests -t .
```

MIT licensed. A [Two Much Labs](https://twomuchlabs.com) project.
