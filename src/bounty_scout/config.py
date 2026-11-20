"""Optional per-repository settings, read from ``.bounty-scout.toml`` in the maintainer's checkout."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

FILENAME = ".bounty-scout.toml"


@dataclass
class Config:
    setup: str | None = None  # run before the test at every commit, e.g. "pip install -e . -q"
    test: str | None = None  # overrides the generated command; may use {test_file}
    good: str | None = None  # known-good ref to bisect from
    timeout: float = 120
    sandbox: str = "worktree"
    docker_image: str = "python:3.12-slim"
    docker_network: str = "none"
    pass_env: list[str] = field(default_factory=list)
    trusted_associations: list[str] = field(default_factory=lambda: ["OWNER", "MEMBER", "COLLABORATOR"])
    ai: str = "off"  # "off" | "anthropic" (needs ANTHROPIC_API_KEY) | "command"
    ai_model: str | None = None
    ai_command: str | None = None  # reads the prompt on stdin, prints the answer
    ai_attempts: int = 2


def load(repo_path: str | Path) -> Config:
    path = Path(repo_path) / FILENAME
    if not path.exists():
        return Config()
    data = tomllib.loads(path.read_text())
    known = {f.name for f in fields(Config)}
    unknown = sorted(set(data) - known)
    if unknown:
        raise ValueError(f"{FILENAME}: unknown key(s): {', '.join(unknown)}")
    return Config(**data)
