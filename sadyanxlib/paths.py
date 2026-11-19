"""Where everything lives.

CODE   the harness repository (this code, prompts, slash command).
ROOT   config/, tasks/ and runs/. Defaults to CODE; SADYANX_ROOT overrides it (tests).
STATE  worktrees, research workspaces, the kill switch and pid files. It lives
       outside the harness repository on purpose: Claude Code loads every CLAUDE.md
       from the working directory up to /, so an agent working under the harness
       repo would pick up the harness's own instructions.
"""

from __future__ import annotations

import os
from pathlib import Path

CODE = Path(__file__).resolve().parents[1]


def root() -> Path:
    return Path(os.environ.get("SADYANX_ROOT") or CODE).expanduser().resolve()


def config_dir() -> Path:
    return root() / "config"


def tasks_dir() -> Path:
    return root() / "tasks"


def runs_dir() -> Path:
    return root() / "runs"


def run_dir(task_id: str) -> Path:
    return runs_dir() / task_id


def state_dir() -> Path:
    raw = os.environ.get("SADYANX_STATE") or "~/.sadyanx"
    return Path(raw).expanduser().resolve()


def worktrees_dir() -> Path:
    return state_dir() / "worktrees"


def workspaces_dir() -> Path:
    return state_dir() / "workspaces"


def pids_dir() -> Path:
    return state_dir() / "pids"


def kill_file() -> Path:
    return state_dir() / "KILL"


def prompts_dir() -> Path:
    return CODE / "prompts"


def sadyanx_bin() -> Path:
    return CODE / "bin" / "sadyanx"
