"""Modes instead of models: a task says how hard the work is, config says who does it.

    ultra     hardest work (gets subagents)
    smart     everyday work, the default
    balanced  routine, high-volume work
    cheap     quick checks

`!deep` means ultra and `!fast` means balanced. Effort (how much the model reasons) comes
with the mode, and can be set apart for one task or one run: `!xhigh`, `!max`...

Escalation climbs a ladder, effort first and model last: each fix attempt of a run goes one
step up (smart/high -> smart/xhigh -> smart/max), and only after `max` does it move to the
next mode, never above `escalation_ceiling` (smart by default: ultra is chosen, not reached).
A task rerun after it failed or was blocked starts one step higher per earlier failure.

Each mode also decides the agent's toolset, because tools are the biggest fixed cost of
every turn (measured 2026-11-17, first-turn context of `claude -p`):

    every tool + every MCP (Claude Code default)        ~27.2k tokens
    code tools + web, no MCP, no subagents              ~9.4k
    + Agent (subagents)                                 ~12.0k
    code tools + web + MCP through ToolSearch           ~14.2k
    code tools + MCP WITHOUT ToolSearch                 ~54.4k  <- MCP schemas load inline
"""

from __future__ import annotations

from dataclasses import dataclass, replace

ORDER = ["cheap", "balanced", "smart", "ultra"]
ALIASES = {"deep": "ultra", "fast": "balanced"}
EFFORTS = ["low", "medium", "high", "xhigh", "max"]  # what `claude --effort` accepts

DEFAULT_MODES = {
    "ultra": {"model": "fable", "effort": "xhigh", "subagents": True},
    "smart": {"model": "opus", "effort": "high", "subagents": False},
    "balanced": {"model": "sonnet", "effort": "medium", "subagents": False},
    "cheap": {"model": "haiku", "effort": "low", "subagents": False},
}

# Built-in tools an agent gets. Everything else Claude Code ships (Workflow, Artifact,
# Skill, scheduling, ...) costs ~14k tokens per turn and a harness agent does not use it.
CODE_TOOLS = ["Bash", "Read", "Edit", "Write", "Glob", "Grep", "WebFetch", "WebSearch"]


@dataclass(frozen=True)
class Mode:
    name: str
    model: str
    effort: str
    subagents: bool


def normalize(name: str) -> str:
    key = (name or "").strip().lstrip("!").lower()
    key = ALIASES.get(key, key)
    if key not in ORDER:
        raise ValueError(f"unknown mode '{name}'; use one of {', '.join(ORDER)} (or deep / fast)")
    return key


def up(name: str) -> str:
    i = ORDER.index(normalize(name))
    return ORDER[min(i + 1, len(ORDER) - 1)]


def normalize_effort(name: str) -> str:
    key = (name or "").strip().lstrip("!").lower()
    if key not in EFFORTS:
        raise ValueError(f"unknown effort '{name}'; use one of {', '.join(EFFORTS)}")
    return key


def resolve(name: str, table: dict, effort: str | None = None) -> Mode:
    """A mode from the config table; `effort` replaces the mode's own effort."""
    key = normalize(name)
    spec = {**DEFAULT_MODES[key], **(table.get(key) or {})}
    return Mode(name=key, model=str(spec["model"]), effort=normalize_effort(effort or str(spec["effort"])),
                subagents=bool(spec["subagents"]))


def ladder(base: str, table: dict, effort: str | None = None, ceiling: str = "smart") -> list[Mode]:
    """Every step escalation can climb from `base`: the base mode's effort up to max, then the
    next mode from its own effort up to max, and so on, never into a mode above `ceiling`.
    The base mode itself is always the first rung, even above the ceiling (you chose it)."""
    current = resolve(base, table, effort)
    steps = [replace(current, effort=e) for e in EFFORTS[EFFORTS.index(current.effort):]]
    while True:
        nxt = up(current.name)
        if nxt == current.name or ORDER.index(nxt) > ORDER.index(normalize(ceiling)):
            return steps
        current = resolve(nxt, table)
        steps += [replace(current, effort=e) for e in EFFORTS[EFFORTS.index(current.effort):]]


def for_attempt(base: str, table: dict, attempt: int, *, effort: str | None = None, start: int = 0,
                escalate: bool = True, ceiling: str = "smart") -> Mode:
    """The mode (model + effort) for one agent call: rung `start` of the ladder for the first
    attempt (reruns after failures start higher), one rung up per fix attempt if escalating."""
    steps = ladder(base, table, effort, ceiling)
    rung = start + (attempt - 1 if escalate else 0)
    return steps[min(max(rung, 0), len(steps) - 1)]


def tool_args(task_type: str, mode: Mode, ask: bool = False) -> list[str]:
    """CLI flags that set the agent's toolset.

    Code tasks get no MCP at all: the Jira ticket is already in the task body and /task moves
    its status. Research tasks need MCP (Claude Docs), and then ToolSearch is mandatory: without
    it Claude Code loads every MCP schema inline and the context doubles.

    `ask` (supervised runs) adds AskUserQuestion: a human is there to answer it."""
    tools = list(CODE_TOOLS)
    if ask:
        tools.append("AskUserQuestion")
    if mode.subagents:
        tools.append("Agent")
    if task_type == "research":
        tools.append("ToolSearch")
        return ["--tools", ",".join(tools)]
    return ["--tools", ",".join(tools), "--strict-mcp-config"]
