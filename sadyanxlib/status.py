"""A snapshot of the harness: `sadyanx status [--json]`.

The queue, what is running, the approvals waiting for you, the kill switch, what the guard
did and where the tokens went, in one call. It is what the desktop dashboard (app/) reads, so
the app never parses files itself and the CLI stays the single source of truth. No model runs.

Like `sadyanx list`, it first marks a task whose runner is gone as `killed`.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from collections import Counter, defaultdict

from . import approvals, paths, persona, runner, tasks, usage

TASK_FIELDS = ("title", "status", "type", "repo", "mode", "effort", "phase", "attempts", "cost_usd",
               "jira", "pr_url", "doc_url", "report", "reason", "created", "updated")
RECENT_DENIALS = 15
EVENT = re.compile(r"^\[([^\]]+)\] (.*)$")
# Work the harness does itself, with no model: counted from events.log, never estimated. Steps that
# use a model (reviews, plan check, Jira, Slack) are in the usage numbers instead.
MODEL_FREE = [
    ("gates", "gate runs", re.compile(r"^gate: ")),
    ("setup", "setup commands", re.compile(r"^setup: ")),
    ("verifications", "verifications", re.compile(r"^verification (?:passed|failed)\b")),
    ("pushes", "pushes", re.compile(r"^pushing ")),
    ("prs_opened", "PRs opened", re.compile(r"^PR opened: ")),
    ("ci_results", "CI results read", re.compile(r"^CI: (?!rerunning\b|could not\b|no\b).* on [0-9a-f]{8}\b")),
    ("ci_reruns", "CI reruns", re.compile(r"^CI: rerunning ")),
]
REASON_CHARS = 240


def guard_summary(days: int) -> dict:
    """What the guard decided in the window: totals, categories of what it stopped, the latest denials."""
    since = (dt.datetime.now() - dt.timedelta(days=days)).isoformat()
    verdicts: Counter = Counter()
    stopped: dict[str, Counter] = defaultdict(Counter)
    recent = []
    for f in sorted(paths.runs_dir().glob("*/audit.jsonl")):
        for line in f.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("ts", "") < since:
                continue
            verdict = rec.get("verdict") or "?"
            verdicts[verdict] += 1
            if verdict != "allow":
                stopped[verdict][rec.get("category") or "?"] += 1
                if verdict == "deny":
                    recent.append({"ts": rec.get("ts", ""), "task": f.parent.name, "tool": rec.get("tool", ""),
                                   "category": rec.get("category", ""),
                                   "reason": str(rec.get("reason", ""))[:REASON_CHARS]})
    recent.sort(key=lambda r: r["ts"], reverse=True)
    return {"verdicts": dict(verdicts), "by_category": {v: dict(c) for v, c in stopped.items()},
            "recent_denials": recent[:RECENT_DENIALS]}


def model_free(days: int) -> dict:
    """How many times the harness did each model-free step in the window."""
    since = (dt.datetime.now() - dt.timedelta(days=days)).isoformat()
    counts = {key: 0 for key, _, _ in MODEL_FREE}
    for f in sorted(paths.runs_dir().glob("*/events.log")):
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            m = EVENT.match(line)
            if not m or m.group(1) < since:
                continue
            for key, _, pattern in MODEL_FREE:
                if pattern.match(m.group(2)):
                    counts[key] += 1
                    break
    return counts


def _task_row(t: tasks.Task) -> dict:
    row = {"id": t.id, **{k: t.get(k) for k in TASK_FIELDS}}
    row["cost_usd"] = t.num("cost_usd")
    row["pending"] = [{"id": i["id"], "kind": i.get("kind", ""), "category": i.get("category", ""),
                       "created": i.get("created", ""), "text": approvals.describe(i)}
                      for i in approvals.pending(paths.run_dir(t.id))] if t.status == "needs-approval" else []
    return row


def mood(kill_switch: bool, pending: int, running: int) -> tuple[str, int]:
    """Which persona line fits the harness right now, and its $n."""
    if kill_switch:
        return "kill-switch", 0
    if pending:
        return "waiting", pending
    if running:
        return "working", running
    return "idle", 0


def snapshot(days: int) -> dict:
    rows = tasks.all_tasks()
    for t in rows:
        runner.recover_stale(t)
    task_rows = [_task_row(t) for t in rows]
    task_rows.sort(key=lambda r: r.get("updated") or r.get("created") or "", reverse=True)
    kill = paths.kill_file().exists()
    running = [r["id"] for r in task_rows if r["status"] in ("running", "verifying")]
    pending = sum(len(r["pending"]) for r in task_rows)
    p = persona.load()
    event, n = mood(kill, pending, len(running))
    return {
        "generated": tasks.now(),
        "root": str(paths.root()),  # task `report` paths are relative to it
        "days": days,
        "kill_switch": kill,
        "persona": {"name": p.name, "line": p.line(event, n=n)} if p else None,
        "counts": dict(Counter(r["status"] for r in task_rows)),
        "running": running,
        "pending_approvals": pending,
        "tasks": task_rows,
        "guard": guard_summary(days),
        "model_free": model_free(days),
        "usage": usage.summarize(days),
    }


def as_json(days: int) -> str:
    return json.dumps(snapshot(days), indent=2, ensure_ascii=False, default=list)


def render(days: int) -> str:
    s = snapshot(days)
    counts = ", ".join(f"{n} {k}" for k, n in sorted(s["counts"].items())) or "no tasks"
    v = s["guard"]["verdicts"]
    u = s["usage"]["total"]
    voice = s["persona"]
    lines = [f"{voice['name']}: {voice['line']}"] if voice and voice["line"] else []
    lines += [
        f"kill switch: {'ON' if s['kill_switch'] else 'off'}",
        f"tasks: {counts}",
        f"running: {', '.join(s['running']) or 'nothing'}",
        f"waiting for your approval: {s['pending_approvals']}",
        f"guard, last {s['days']} days: {v.get('allow', 0)} allowed, {v.get('deny', 0)} denied, "
        f"{v.get('ask', 0)} parked for approval",
        f"agents, last {s['days']} days: {u['calls']} calls, {u['tokens']:,} tokens, est ${u['cost_usd']:.2f}",
        "without a model: " + (", ".join(f"{s['model_free'][key]} {label}" for key, label, _ in MODEL_FREE
                                         if s["model_free"][key]) or "nothing yet"),
    ]
    for r in s["tasks"]:
        for p in r["pending"]:
            lines.append(f"  ⏸ {r['id']} {p['text']}")
    return "\n".join(lines)
