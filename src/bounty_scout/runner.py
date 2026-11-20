"""Isolated execution: temporary git worktrees and a scrubbed command runner.

Two isolation levels:

* ``worktree`` – a detached temporary ``git worktree`` plus a scrubbed
  environment and a throw-away ``HOME``. This protects the maintainer's
  checkout and keeps tokens out of the child process. It is *not* a security
  sandbox: the command can still read the filesystem and use the network.
* ``docker`` – the same worktree mounted into a container with networking
  disabled and CPU/memory/PID limits. Use this for code from untrusted issues.
"""

from __future__ import annotations

import hashlib
import os
import shlex
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

MAX_OUTPUT = 20_000  # characters kept per run (head and tail)
SETUP_FAILED = "__SCOUT_SETUP_FAILED__"


class GitError(RuntimeError):
    pass


def git(repo: str | Path, *args: str, check: bool = True, timeout: float | None = 120) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, errors="replace", timeout=timeout,
    )
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed ({proc.returncode}): {proc.stderr.strip() or proc.stdout.strip()}")
    return proc


class Worktree:
    """A detached temporary worktree of ``repo`` at ``ref``, removed on exit."""

    def __init__(self, repo: str | Path, ref: str, keep: bool = False):
        self.repo = Path(repo).resolve()
        self.ref = ref
        self.keep = keep
        self._tmp: Path | None = None
        self.path: Path = Path()

    def __enter__(self) -> "Worktree":
        self._tmp = Path(tempfile.mkdtemp(prefix="scout-wt-"))
        self.path = self._tmp / "wt"
        try:
            git(self.repo, "worktree", "add", "--detach", "--force", str(self.path), self.ref)
        except Exception:
            shutil.rmtree(self._tmp, ignore_errors=True)
            raise
        return self

    def __exit__(self, *exc) -> None:
        if self.keep:
            return
        git(self.repo, "worktree", "remove", "--force", str(self.path), check=False)
        shutil.rmtree(self._tmp, ignore_errors=True)
        git(self.repo, "worktree", "prune", check=False)

    def reset(self) -> None:
        """Drop everything a command left behind so the next checkout cannot fail."""
        reset_tree(self.path)


def reset_tree(path: str | Path) -> None:
    git(path, "reset", "--hard", "--quiet", check=False)
    git(path, "clean", "-fdxq", check=False)


def scrubbed_env(home: str | Path, extra: dict[str, str] | None = None, pass_env: list[str] | None = None) -> dict[str, str]:
    """A minimal environment: no tokens, no credentials helper, throw-away HOME."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": str(home),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "TMPDIR": str(home),
        "PYTHONDONTWRITEBYTECODE": "1",  # stale .pyc files would poison later bisect steps
        "PYTHONUNBUFFERED": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "CI": "1",
    }
    for name in pass_env or []:
        if name in os.environ:
            env[name] = os.environ[name]
    env.update(extra or {})
    return env


@dataclass
class RunResult:
    exit_code: int
    output: str
    timed_out: bool
    duration: float


def _truncate(text: str, limit: int = MAX_OUTPUT) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n… [{len(text) - limit} characters truncated] …\n{text[-half:]}"


def run(argv: list[str], cwd: str | Path, env: dict[str, str], timeout: float) -> RunResult:
    """Run ``argv`` in its own process group; kill the whole group on timeout."""
    start = time.monotonic()
    proc = subprocess.Popen(
        argv, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, errors="replace", start_new_session=True,
    )
    timed_out = False
    try:
        output, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        output, _ = proc.communicate()
    return RunResult(proc.returncode, _truncate(output or ""), timed_out, time.monotonic() - start)


def compose_script(command: str, setup: str | None) -> str:
    """One shell script: optional setup (failure => 125, i.e. "skip"), then the test."""
    if not setup:
        return command
    return f"( {setup} ) >/dev/null 2>&1 || {{ echo {SETUP_FAILED}; exit 125; }}\n{command}"


def docker_argv(
    script: str, worktree: str | Path, repro_dir: str | Path | None, image: str,
    network: str = "none", env: dict[str, str] | None = None, name: str | None = None,
    timeout: float | None = None,
) -> list[str]:
    argv = [
        "docker", "run", "--rm", "--network", network,
        "--cpus", "2", "--memory", "2g", "--pids-limit", "512",
        "--security-opt", "no-new-privileges", "--cap-drop", "ALL",
        "--user", f"{os.getuid()}:{os.getgid()}",  # files left in the worktree stay removable
        "-v", f"{Path(worktree).resolve()}:/work", "-w", "/work",
    ]
    if name:
        argv += ["--name", name]
    if repro_dir:
        argv += ["-v", f"{Path(repro_dir).resolve()}:/scout:ro"]
    for key, value in (env or {}).items():
        argv += ["-e", f"{key}={value}"]
    # Enforce the time limit inside the container too, so a hung test does not wait on the outer grace period.
    limit = ["timeout", "-k", "2", str(max(1, int(timeout)))] if timeout else []
    return argv + [image, *limit, "sh", "-c", script]


def ensure_image(base: str, packages: list[str]) -> str:
    """A local image that is ``base`` plus the maintainer's pip packages. Built once, then reused.

    The build needs the network; the tests that later run in the image do not get it.
    """
    if not packages:
        return base
    digest = hashlib.sha1((base + "\n" + "\n".join(sorted(packages))).encode()).hexdigest()[:12]
    tag = f"scout-env:{digest}"
    if subprocess.run(["docker", "image", "inspect", tag], capture_output=True).returncode == 0:
        return tag
    dockerfile = f"FROM {base}\nRUN pip install --no-cache-dir {' '.join(shlex.quote(p) for p in packages)}\n"
    build = subprocess.run(["docker", "build", "-q", "-t", tag, "-"], input=dockerfile, capture_output=True, text=True)
    if build.returncode != 0:
        raise RuntimeError(f"could not build the sandbox image from {base} with {packages}: {build.stderr.strip()[-400:]}")
    return tag


def run_isolated(
    command: str, worktree: str | Path, *, setup: str | None = None, timeout: float = 120,
    sandbox: str = "worktree", repro_dir: str | Path | None = None,
    docker_image: str = "python:3.12-slim", docker_network: str = "none",
    pass_env: list[str] | None = None,
) -> RunResult:
    """Run ``command`` against ``worktree`` under the chosen isolation level."""
    worktree = Path(worktree)
    script = compose_script(command, setup)
    home = Path(tempfile.mkdtemp(prefix="scout-home-"))
    try:
        if sandbox == "docker":
            inner = {"PYTHONPATH": "/work:/work/src", "PYTHONDONTWRITEBYTECODE": "1", "HOME": "/tmp", "CI": "1"}
            name = f"scout-{os.getpid()}-{int(time.time() * 1000) % 10**9}"
            argv = docker_argv(script, worktree, repro_dir, docker_image, docker_network, inner, name, timeout)
            # The docker CLI needs the caller's HOME/DOCKER_* to find the daemon; the container sees none of it.
            env = scrubbed_env(os.environ.get("HOME", str(home)), pass_env=["DOCKER_HOST", "DOCKER_CONTEXT", "XDG_RUNTIME_DIR"])
            result = run(argv, worktree, env, timeout + 60)  # grace for image pull and container start
            if result.timed_out:
                subprocess.run(["docker", "kill", name], capture_output=True)
            elif result.exit_code in (124, 137):  # coreutils timeout: TERM, then KILL
                result.timed_out = True
            return result
        if sandbox != "worktree":
            raise ValueError(f"unknown sandbox {sandbox!r} (expected 'worktree' or 'docker')")
        pythonpath = os.pathsep.join(str(p) for p in (worktree, worktree / "src") if p.exists())
        env = scrubbed_env(home, {"PYTHONPATH": pythonpath}, pass_env)
        return run(["sh", "-c", script], worktree, env, timeout)
    finally:
        shutil.rmtree(home, ignore_errors=True)


def quote(path: str | Path) -> str:
    return shlex.quote(str(path))
