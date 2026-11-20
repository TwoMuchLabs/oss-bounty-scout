# oss-bounty-scout

A background triage agent for GitHub maintainers. For each newly opened issue it:

1. **Parses** the issue: reproduction steps, runnable code, error logs, stack frames, environment, version hints.
2. **Reproduces** it in a temporary git worktree (or a network-less container), running the reporter's code as a failing test.
3. **Bisects** with `git bisect` to the commit that introduced the regression.
4. **Reports** in Markdown: reproduction status, culprit commit, suspected file and line, and a candidate fix diff that has been re-tested against the reproduction.

Nothing is posted to GitHub unless you pass `--publish`.

No AI model, no API keys and no dependencies beyond Python 3.11+, `git` and the [GitHub CLI](https://cli.github.com), unless you [turn AI on](#optional-ai-help). By default everything is deterministic and runs on your machine or your CI runner.

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
ai = "off"                       # "anthropic", "openai" or "command" to turn AI help on
```

## How a commit is judged

The same check is used to confirm the bug, find a good baseline, drive `git bisect run`, and verify the fix:

- **bad**: the reported error appears in the output: the same exception type with the same or a similar message, since wording, paths and line numbers differ between machines and Python versions (or, with no known error, the command exits non-zero)
- **good**: the command exits 0 without it
- **skip**: setup failed, the run timed out, or it failed with a *different* error

The reporter's snippet is run exactly as written. Two things are done for it first, both from a static read of your code (nothing is imported or executed): imports the snippet forgot (`import os`, `from yourpkg import Thing`) are added and listed in the report, and `python app.py --flag` in the report keeps its arguments.

Report statuses: `reproduced`, `not-reproduced`, `inconclusive` (failed differently), `needs-info` (nothing runnable in the issue), `needs-approval` (see below).

## Optional AI help

Off by default. About nine in ten bug reports contain no code that runs, and undoing a commit is rarely the right fix. Turn AI on and a model may propose two things:

- **A test**, when the report has no runnable code or its code fails differently. It is used only if it fails with the error the reporter described.
- **A patch**, when the revert does not fix the test. It is used only if the failing test then passes. It may not touch tests or files outside the project.

Anything that does not pass its check is thrown away, and the model gets one more try with the reason. Everything it wrote is labelled "Written by an AI model" in the report, and the footer names the provider and model and how many calls were made.

Use whichever provider you like. The rest of the tool does not know or care which one it is:

| Provider | Turn it on | Key |
|---|---|---|
| Anthropic | `--ai anthropic` | `ANTHROPIC_API_KEY` |
| OpenAI | `--ai openai` | `OPENAI_API_KEY` |
| Anything that speaks OpenAI's chat protocol (Azure, OpenRouter, Ollama, vLLM, LM Studio…) | `--ai openai --ai-base-url http://localhost:11434/v1 --ai-model <name>` | `OPENAI_API_KEY` if the server wants one |
| Anything else | `--ai-cmd "your-command"` (prompt on stdin, answer on stdout) | yours |

`--ai-model` picks the model; each provider has a default (`claude-sonnet-5-5`, `gpt-6.2-astra`). The same settings work in `.bounty-scout.toml` as `ai`, `ai_model`, `ai_base_url` and `ai_command`.

What changes when you turn it on:

- Parts of the issue and of your source code are sent to the provider you chose. With a local server or your own command, they stay with you.
- It costs money on a hosted provider: up to four calls per issue.
- Results are no longer exactly repeatable.
- A test with no reported error to match is the weak case. It counts only if some earlier version passes it; otherwise the report says it proves nothing.
- A patch that passes one test can still be wrong. Review it like any contribution from a stranger.

The model is never asked when the reporter's own code already reproduces or runs cleanly, and never for an untrusted author outside the container. Keys stay in the scout's process and are not visible to the code under test.

Status: the checks, and each provider's request and response format, are tested against stand-ins. No real model from any provider has been run through it yet.

## Safety

Issues contain code written by strangers. By default, code from an issue is executed only when its author is an `OWNER`, `MEMBER` or `COLLABORATOR`; otherwise the report shows the generated test and stops at `needs-approval`.

- The worktree runner protects your checkout and scrubs the environment (no tokens, throw-away `HOME`). It is **not** a security sandbox.
- For untrusted code use `--sandbox docker`: no network, dropped capabilities, CPU/memory/PID limits, non-root user.
- Your working tree, index and any bisect you have in progress are never touched.

## GitHub Action

Copy [`templates/bounty-scout.yml`](templates/bounty-scout.yml) to `.github/workflows/` in your repository. It runs when a maintainer opens an issue, when the `scout` label is added, or by hand for any issue number, keeps the token out of the step that executes issue code, and writes the report to the job summary and an artifact. Set the repository variable `SCOUT_PUBLISH=true` to have it comment on the issue.

## How well does it work?

We replayed closed issues from eight public Python projects (click, jinja, werkzeug, attrs, marshmallow, rich, tomlkit, packaging) as if each had just been opened: the scout ran at the commit that was current that day, in the Docker sandbox, on GitHub's runners. Then we scored it against what the maintainers actually did. Run of 20 November 2026:

| | |
|---|---|
| Closed issues scanned | 2,317 |
| Had a Python snippet and an error to match | 213 |
| **Reproduced** | **79** |
| Ran cleanly (bug not on the main branch that day, or not triggered) | 51 |
| Failed for another reason | 83, of which 41 needed a third-party package the sandbox did not have |
| Reproductions we could check against the maintainers' fix | 66, of which **46 pass once the fix is in** |
| **Culprit commit found** | **21** (median: 7 test runs to search 117 commits) |
| No earlier version where it worked | 52 (not a regression, or older versions no longer run) |
| Search stopped or narrowed to a few commits | 6 |
| Suspect file is a file the maintainers' fix changed | **12 of 16** that could be scored |
| Suggested patch applied and made the test pass | 5 of 21 |

Read it this way: about one closed issue in ten carries something runnable, a bit over a third of those reproduce unattended, and about a quarter of the reproduced ones get a culprit commit. Two of the 21 culprits are named by maintainers in the issue thread; the others have not been independently confirmed, though titles such as "remove deprecated Markup and escape aliases" for "cannot import name 'escape'" speak for themselves. The patch is the weak part: a revert usually no longer applies once the file has moved on.

The first replay did worse (59 reproduced, 13 culprits) and showed why. Those fixes are in this version. Reproduce the numbers with the `Field test` workflow, or `fieldtest/field_test.py`.

## Limits

- Reproduction needs a runnable Python or Node snippet, shell commands, or your `--test-cmd`. Bugs that show up as wrong output rather than an error need `--test-cmd`.
- The sandbox image needs your project's dependencies, and anything else the snippet imports.
- The failing test is the reporter's snippet; it is not minimised.
- The suggested fix is a revert of the culprit's change (to the suspect file, else the whole commit), merged onto the failing commit when it can be. It restores old behaviour and is a starting point for a real fix.
- Bisecting stops when more than 20 commits in the range cannot be tested. Pass a nearer `--good`.
- Field-tested on Python projects only. Node snippets and shell steps have unit tests but no field test. POSIX only.

## Development

```sh
pip install -e .
python -m unittest discover -s tests -t .
```

MIT licensed. A [Two Much Labs](https://twomuchlabs.com) project.
