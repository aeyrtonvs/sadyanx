"""A task's timeline: `sadyanx log <task> --json`.

One ordered list of what happened to a task, merged from the files its runs leave in
runs/<task>/: events.log (what the runner did), usage.jsonl (each agent call: phase, mode,
tokens, cost, time), approvals.json (what was parked and how you decided) and audit.jsonl
(what the guard denied). The dashboard shows it beside the report, to see why a task cost
what it did or where it went wrong without opening the files. Read-only; no model runs.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from . import approvals, paths, usage

MAX_EVENTS = 400   # the latest ones; `truncated` says when older ones were left out
TEXT_CHARS = 300
DETAIL_CHARS = 1200
EVENT = re.compile(r"^\[([^\]]+)\] (.*)$")

# events.log lines, by what they are about. First match wins; anything else is a "note".
KINDS = [
    ("start", re.compile(r"^=== task |^(?:reusing )?worktree ")),
    ("final", re.compile(r"^final status: ")),
    ("error", re.compile(r"^(?:error|fatal|warning): |^the PR could not be opened|^stopping the agent|"
                         r"^no result: |^keep awake: ")),
    ("agent", re.compile(r"^agent(?: finished)?: |^attempt \d+: |^rerun after ")),
    ("gate", re.compile(r"^(?:gate|setup): ")),
    ("check", re.compile(r"^(?:verification|review|code review|plan check)\b|^publishing with ")),
    ("ship", re.compile(r"^pushing |^PR (?:opened|round|review)|^external PR review|^pr_(?:ci|review): ")),
    ("ci", re.compile(r"^CI: |^waiting for ")),
    ("notify", re.compile(r"^(?:slack|jira): ")),
]
# The same approval also sits in approvals.json, with its decision: show it once, from there.
PARKED = re.compile(r"waiting for approval \[[0-9a-f]{8}\]")
GOOD = re.compile(r"^(?:verification passed|review passed|final status: done|PR opened|slack: reply sent|"
                  r"plan check: as expected|CI: .* passed on |jira: .* moved)")
BAD = re.compile(r"^(?:verification failed|review failed|final status: (?:failed|killed|blocked)|"
                 r"CI: .* (?:failed|error) on |slack: (?:reply NOT|no reply) sent)")


def _clip(text: str, n: int) -> str:
    text = str(text)
    return text if len(text) <= n else text[: n - 1] + "…"


def _tone(kind: str, text: str) -> str:
    if kind == "error" or BAD.match(text):
        return "bad"
    if GOOD.match(text):
        return "good"
    return ""


def from_events(f: Path) -> list[dict]:
    """events.log: one event per `[ts] message` line; lines without a timestamp (a command's
    output, a multi-line error) belong to the event above them."""
    out: list[dict] = []
    if not f.exists():
        return out
    for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
        m = EVENT.match(line)
        if not m:
            if out and line.strip():
                out[-1]["detail"] = _clip((out[-1].get("detail", "") + "\n" + line).lstrip("\n"), DETAIL_CHARS)
            continue
        ts, text = m.group(1), m.group(2)
        if PARKED.search(text):
            continue
        kind = next((k for k, pattern in KINDS if pattern.match(text)), "note")
        out.append({"ts": ts, "kind": kind, "tone": _tone(kind, text), "text": _clip(text, TEXT_CHARS)})
    return out


def from_usage(f: Path) -> list[dict]:
    """usage.jsonl: one event per agent call, with its numbers."""
    out = []
    if not f.exists():
        return out
    for line in f.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(r, dict) or not r.get("ts"):
            continue
        who = f"{r.get('mode') or '?'} · {r.get('model') or '?'}/{r.get('effort') or '?'}"
        out.append({"ts": r["ts"], "kind": "call", "tone": "",
                    "text": _clip(f"{r.get('phase') or 'agent'} call, attempt {r.get('attempt') or '?'} ({who}): "
                                  f"{r.get('outcome') or 'no outcome'}", TEXT_CHARS),
                    "tokens": usage.total_tokens(r), "cost_usd": float(r.get("cost_usd") or 0),
                    "seconds": int(r.get("seconds") or 0)})
    return out


def from_approvals(run_dir: Path) -> list[dict]:
    """approvals.json: when each action was parked, and when you approved or rejected it."""
    out = []
    for item in approvals.load(run_dir):
        if not isinstance(item, dict) or not item.get("id"):
            continue
        try:
            what = approvals.describe(item)
        except (KeyError, TypeError, AttributeError):  # a hand-edited or older entry
            what = f"[{item['id']}] {item.get('category') or 'approval'}"
        if item.get("created"):
            out.append({"ts": item["created"], "kind": "approval", "tone": "warn",
                        "text": _clip("parked for your approval: " + what.splitlines()[0], TEXT_CHARS),
                        "detail": _clip(what, DETAIL_CHARS)})
        if item.get("decided") and item.get("status") in ("approved", "rejected", "consumed"):
            verb = "rejected" if item["status"] == "rejected" else "approved"
            note = f" ({item['note']})" if item.get("note") else ""
            out.append({"ts": item["decided"], "kind": "approval", "tone": "bad" if verb == "rejected" else "good",
                        "text": _clip(f"you {verb} [{item['id']}]{note}", TEXT_CHARS)})
    return out


def from_audit(f: Path) -> list[dict]:
    """audit.jsonl: only what the guard denied (allowed calls are the noise; parked ones are approvals)."""
    out = []
    if not f.exists():
        return out
    for line in f.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(r, dict) and r.get("verdict") == "deny" and r.get("ts"):
            out.append({"ts": r["ts"], "kind": "guard", "tone": "bad",
                        "text": _clip(f"guard denied {r.get('tool') or '?'} ({r.get('category') or '?'}): "
                                      f"{r.get('reason') or ''}", TEXT_CHARS)})
    return out


def build(task_id: str) -> dict:
    d = paths.run_dir(task_id)
    events = (from_events(d / "events.log") + from_usage(d / "usage.jsonl") + from_approvals(d)
              + from_audit(d / "audit.jsonl"))
    events.sort(key=lambda e: e["ts"])  # stable: within one second, each file keeps its own order
    return {"task": task_id, "events": events[-MAX_EVENTS:], "truncated": len(events) > MAX_EVENTS}
