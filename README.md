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
docker_image = "python:{python}-slim"            # {python}: the version in the report, else the era of the commit
docker_packages = ["pytest", "requests"]         # pip packages baked into a derived image, built once
timeout = 180
ai = "off"                       # "anthropic", "openai" or "command" to turn AI help on
```

## How a commit is judged

The same check is used to confirm the bug, find a good baseline, drive `git bisect run`, and verify the fix:

- **bad**: the reported error appears in the output. If the reported line itself shows up at the failing commit, only that line counts from then on. If not, the same exception type with a similar message counts, since wording, paths and line numbers differ between machines and Python versions; quoted names must still agree, so `NameError: 'foo'` never stands in for `NameError: 'bar'`. With no known error, the command exiting non-zero counts
- **good**: the command exits 0 without it
- **skip**: setup failed, the run timed out, or it failed with a *different* error

The reporter's snippet is run exactly as written, with a few things people leave out filled in from a static read of your code (nothing of yours is imported or executed), each listed in the report: forgotten imports (`import os`, `from typing import Optional`, `from yourpkg import Thing`, `import yourpkg.sub`); the arguments from `python app.py --flag`; calls to test functions the snippet defines but never calls; and pytest, when the report shows pytest output.

Report statuses: `reproduced`, `not-reproduced`, `inconclusive` (failed differently), `needs-info` (nothing runnable in the issue), `needs-approval` (see below), `unsupported` (the report is about a type checker's output, not about running code).

A reproduced bug is also classed as a regression or not. If no earlier version passes the test, the report says whether that is because the oldest testable version already fails (probably not a regression) or because older versions could not be run at all.

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

### Getting dependencies into the sandbox

The container has no network while tests run, so whatever a snippet imports must already be in the image. Three ways, simplest first: list pip packages in `docker_packages` (a derived image is built once and reused); point `docker_image` at your own CI image; or put `{python}` in the image name to get the Python version the reporter used, which matters for old reports whose code no longer runs on a current Python. JavaScript snippets run on `node:22-slim` unless you choose an image, and `require('your-package')` or `import ... from 'your-package'` resolves to the checkout under test.

## Safety

Issues contain code written by strangers. By default, code from an issue is executed only when its author is an `OWNER`, `MEMBER` or `COLLABORATOR`; otherwise the report shows the generated test and stops at `needs-approval`.

- The worktree runner protects your checkout and scrubs the environment (no tokens, throw-away `HOME`). It is **not** a security sandbox.
- For untrusted code use `--sandbox docker`: no network, dropped capabilities, CPU/memory/PID limits, non-root user.
- Your working tree, index and any bisect you have in progress are never touched.
- One thing runs outside the sandbox, in the scout's own process: when a snippet uses a name it never imports and that name is a Python standard-library module, the scout imports that module to choose between `import x` and `from x import x`. No code from the issue runs, but a few standard modules do something on import: `this` prints a poem into the scout's output, and `antigravity` asks the system to open a web browser. It happens for any report whose code is allowed to run, which with `--sandbox docker` means reports from anyone. Known and left as it is; see Limits.

## GitHub Action

Copy [`templates/bounty-scout.yml`](templates/bounty-scout.yml) to `.github/workflows/` in your repository. It runs when a maintainer opens an issue, when the `scout` label is added, or by hand for any issue number, keeps the token out of the step that executes issue code, and writes the report to the job summary and an artifact. Set the repository variable `SCOUT_PUBLISH=true` to have it comment on the issue.

## How well does it work?

We replay closed issues from public projects as if each had just been opened: the scout runs at the commit that was current that day, in the Docker sandbox, on GitHub's runners. Then we score it against what the maintainers actually did.

Two sets of projects. The **tuned** set (click, jinja, werkzeug, attrs, marshmallow, rich, tomlkit, packaging) is where we read the failures and improved the tool, so its numbers flatter it. The **holdout** set (sqlparse, pyparsing, arrow, tqdm, isort, pygments, more-itertools, and three JavaScript projects: commander.js, minimist, node-semver) was added afterwards and never tuned on. Latest run, 20 November 2026, AI off:

| | Tuned | Holdout |
|---|---|---|
| Closed issues scanned | 2,317 | 2,727 |
| Had a runnable snippet and an error to match | 230 | 158 |
| **Reproduced** | **97** (42%) | **45** (28%) |
| Ran cleanly | 54 | 56 |
| Failed for another reason | 73 | 57 |
| Regressions (an earlier version passes the test) | 36 | 15 |
| Not regressions (the oldest testable version already fails) | 59 | 29 |
| **Culprit commit found** | **26** | **10** |
| ...of which maintainers name that commit in the thread | 2 | 3 |
| Suspect file is one the maintainers' fix changed (culprit cases) | 13 of 20 | 8 of 8 |
| Suggested patch made the test pass | 9 | 5 |

**Is a reproduction the real bug?** Where the issue was closed by a commit that changed code, we re-ran the test after that commit:

| | Tuned | Holdout |
|---|---|---|
| Test passes once the maintainers' fix is in | 44 | 14 |
| Still fails | 5 | 9 |
| Unclear (fails differently) | 7 | 5 |

So on projects it was not tuned on, a third of the checkable reproductions were not fixed by the fix. Reading those cases, the usual reason is that the snippet shows behaviour the maintainers kept: the issue was really about a confusing message, missing documentation or a feature request, and the "error" is still raised by design. The report carries a **confidence** rating for exactly this reason:

| Confidence | When | Fix agreed (both sets) |
|---|---|---|
| High | an earlier version passes the test, the reported error appears exactly, and it is raised inside the project | 10 of 12 |
| Medium | one of those is missing | 12 of 12 |
| Low | two or more are missing, typically "every version fails the same way" | 36 of 48 |

Most reproductions are rated low (110 of 142), because most reproduced bugs are not regressions. Treat a low rating as "the snippet does raise this error", not as "this is a bug".

JavaScript is the weakest area: 14 runnable reports across three projects, 2 reproduced. Most snippets were fragments that leave out the `require` line or need a companion package.

Each round of reading failures has moved the tuned-set numbers: 59 reproduced and 13 culprits at first, then 79 and 21, 88 and 23, now 97 and 26. The replay runs on every change to the tool and fails if one of the 36 culprits it has found is lost (`fieldtest/golden.json`). Reproduce it with the `Field test` workflow, or `fieldtest/field_test.py`.

## Limits

- Reproduction needs a runnable Python or Node snippet, shell commands, or your `--test-cmd`. Bugs that show up as wrong output rather than an error need `--test-cmd`.
- The sandbox image needs your project's dependencies, and anything else the snippet imports.
- The failing test is the reporter's snippet; it is not minimised.
- The suggested fix undoes the culprit's change: one hunk if that is enough, else the suspect file, else the whole commit, merged onto the failing commit when it can be. It restores old behaviour and is a starting point for a real fix.
- Bisecting stops when more than 20 commits in the range cannot be tested. Pass a nearer `--good`.
- Field-tested mostly on Python. JavaScript works for complete snippets but few real reports are complete; TypeScript is not handled. Shell steps have unit tests but no field test. POSIX only.
- The project runs from its source tree, not installed, unless your `setup` installs it. Bugs that depend on package metadata can look different.
- Filling in a forgotten import loads the named standard-library module in the scout's own process (see Safety). A report that uses `this` or `antigravity` as a bare name therefore prints text into the run's output or tries to open a browser. Nothing from the issue is executed by this, and the triage result is not affected.

## Development

```sh
pip install -e .
python -m unittest discover -s tests -t .
```

### Checking a refactor

"No behaviour change" should be a measurement. `scripts/behaviour_check.py` runs the code from a git ref and the code in the working tree over the same inputs and compares everything they produce:

```sh
python scripts/behaviour_check.py <base-ref> --issues issues.json.gz --fetch fetch.json.gz --repo <python-checkout> [--repo ...] [--only text,repositories,reports,field,triage,cli]
```

`issues.json.gz` (1,398 real issues from twelve projects) and `fetch.json.gz` are on the `recordings` branch, written by the `Record GitHub issues` workflow. The check covers the parser and the reproduction builder on every recorded issue and damaged copies of it, error matching, import completion and localisation against the checkouts you give it, the whole pipeline on small purpose-built repositories (AI gates included, with canned replies), the report for 6,000 made-up results, and the command line. A clean result is evidence, not proof: the script's header lists what it does not cover. The `Field test` workflow remains the check that the tool still finds the culprits it used to find.

MIT licensed. A [Two Much Labs](https://twomuchlabs.com) project.
