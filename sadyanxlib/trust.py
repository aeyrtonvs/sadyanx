"""Claude Code's folder trust, for supervised runs.

An interactive `claude` asks "Do you trust this folder?" the first time it opens one, with
"No, exit" as the default; headless runs (`claude -p`) never ask. Observed 2026-11-17 with
Claude Code 2.1.326:

- A git worktree is trusted through its **main repo**: accepting the dialog inside a worktree
  stores the trust under the main clone's path, and a trusted parent folder of the repo does
  not count. So code tasks need each registered repo trusted (usually already true: you have
  opened Claude there).
- A folder outside git (a research workspace) is trusted when it or any parent is. So
  trusting the harness's state dir (`~/.sadyanx`) once covers every workspace.

The flag lives in Claude Code's own config (`~/.claude.json`, or `$CLAUDE_CONFIG_DIR/.claude.json`)
as projects[<path>].hasTrustDialogAccepted. Only `sadyanx install` writes it, and only that key.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def config_path() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(base) / ".claude.json" if base else Path.home() / ".claude.json"


def _projects() -> dict:
    try:
        data = json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    projects = data.get("projects") if isinstance(data, dict) else None
    return projects if isinstance(projects, dict) else {}


def _accepted(projects: dict, path: Path) -> bool:
    entry = projects.get(str(path))
    return isinstance(entry, dict) and bool(entry.get("hasTrustDialogAccepted"))


def trusted(path: Path, git_repo: Path | None = None) -> bool:
    """Whether an interactive claude opened in `path` will start without asking.

    `git_repo`: the main clone when `path` is one of its worktrees (code tasks); then only
    that repo's own entry counts. Otherwise `path` or any parent counts."""
    projects = _projects()
    if git_repo is not None:
        return _accepted(projects, Path(os.path.realpath(git_repo)))
    p = Path(os.path.realpath(path))
    return any(_accepted(projects, candidate) for candidate in (p, *p.parents))


def trust(path: Path) -> bool:
    """Mark `path` itself trusted. Returns False if it already was. Writes atomically and
    touches nothing else in the file; refuses to write if the file exists but cannot be parsed."""
    projects = _projects()
    key = os.path.realpath(path)
    if _accepted(projects, Path(key)):
        return False
    cfg = config_path()
    if cfg.exists():
        data = json.loads(cfg.read_text(encoding="utf-8"))  # a broken file is not ours to rewrite
        if not isinstance(data, dict):
            raise ValueError(f"{cfg} is not a JSON object")
    else:
        data = {}
    projects = data.setdefault("projects", {})
    entry = projects.get(key) if isinstance(projects.get(key), dict) else {}
    projects[key] = {**entry, "hasTrustDialogAccepted": True}
    fd, tmp = tempfile.mkstemp(dir=str(cfg.parent), prefix=".claude.json.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
        if cfg.exists():
            os.chmod(tmp, cfg.stat().st_mode & 0o777)
        os.replace(tmp, cfg)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return True
