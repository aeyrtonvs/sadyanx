"""The voice sadyanx uses with you: config/persona.toml (local; persona.example.toml ships).

The persona only talks to you: notifications, `sadyanx status`, the dashboard and the /task
conversation. It never reaches an agent's prompt (agents stay terse, and a persona there would
cost tokens on every turn) or anything other people read (Slack replies, PRs, Jira, task files).

Lines are templates picked at random, so no model runs and no tokens are spent. A line adds to
the facts and never replaces them. A missing, disabled or broken file means no persona: the
harness never fails over its voice.
"""

from __future__ import annotations

import random
import string
import tomllib
from dataclasses import dataclass, field

from . import paths

FILENAME = "persona.toml"
MAX_LINE = 160
# events a line can be written for, and the placeholders each one gets
EVENTS = {
    "done": "$id $title $pr",
    "failed": "$id $title $reason",
    "blocked": "$id $title $reason",
    "needs-approval": "$id $title $n",
    "killed": "$id $title",
    "login": "$id $title",
    "idle": "(status, nothing running or waiting)",
    "working": "$n (tasks running)",
    "waiting": "$n (approvals waiting)",
    "kill-switch": "(the kill switch is on)",
}


@dataclass
class Persona:
    name: str
    voice: str = ""
    language: str = ""
    lines: dict[str, list[str]] = field(default_factory=dict)

    def line(self, event: str, rng: random.Random | None = None, **fields) -> str:
        """A random line for the event with its placeholders filled in, or "" if there is none."""
        options = [str(o).strip() for o in self.lines.get(event, []) if str(o).strip()]
        if not options:
            return ""
        values = {"name": self.name, **{k: "" if v is None else str(v) for k, v in fields.items()}}
        text = string.Template((rng or random).choice(options)).safe_substitute(values)
        text = " ".join(text.split())
        return text if len(text) <= MAX_LINE else text[:MAX_LINE - 1].rstrip() + "…"


def load() -> Persona | None:
    f = paths.config_dir() / FILENAME
    try:
        with f.open("rb") as fh:
            raw = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return None
    if not raw.get("enabled", True) or not str(raw.get("name", "")).strip():
        return None
    lines = raw.get("lines") if isinstance(raw.get("lines"), dict) else {}
    lines = {str(k): [v] if isinstance(v, str) else list(v) for k, v in lines.items() if isinstance(v, (str, list))}
    return Persona(name=str(raw["name"]).strip(), voice=str(raw.get("voice", "")).strip(),
                   language=str(raw.get("language", "")).strip(), lines=lines)


def line(event: str, **fields) -> str:
    p = load()
    return p.line(event, **fields) if p else ""


def describe() -> str:
    """For /task: who to be when talking to the user, or that there is no persona."""
    p = load()
    if not p:
        return f"no persona (config/{FILENAME} is missing, disabled or unreadable)"
    out = [f"name: {p.name}"]
    if p.language:
        out.append(f"language: {p.language}")
    if p.voice:
        out.append("voice:\n" + "\n".join("  " + ln for ln in p.voice.splitlines()))
    return "\n".join(out)
