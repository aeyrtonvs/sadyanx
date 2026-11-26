"""A snapshot of the harness: `sadyanx status [--json]`.

The queue, what is running, the approvals waiting for you, the kill switch, what the guard
did and where the tokens went, in one call. It is what the desktop dashboard (app/) reads, so
the app never parses files itself and the CLI stays the single source of truth. No model runs.

Like `sadyanx list`, it first marks a task whose runner is gone as `killed`.
"""

from __future__ import annotations

import datetime as dt
import json
from collections import Counter, defaultdict

from . import approvals, paths, runner, tasks, usage

TASK_FIELDS = ("title", "status", "type", "repo", "mode", "effort", "phase", "attempts", "cost_usd",
               "jira", "pr_url", "doc_url", "report", "reason", "created", "updated")
RECENT_DENIALS = 15
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


def _task_row(t: tasks.Task) -> dict:
    row = {"id": t.id, **{k: t.get(k) for k in TASK_FIELDS}}
    row["cost_usd"] = t.num("cost_usd")
    row["pending"] = [{"id": i["id"], "kind": i.get("kind", ""), "category": i.get("category", ""),
                       "created": i.get("created", ""), "text": approvals.describe(i)}
                      for i in approvals.pending(paths.run_dir(t.id))] if t.status == "needs-approval" else []
    return row


def snapshot(days: int) -> dict:
    rows = tasks.all_tasks()
    for t in rows:
        runner.recover_stale(t)
    task_rows = [_task_row(t) for t in rows]
    task_rows.sort(key=lambda r: r.get("updated") or r.get("created") or "", reverse=True)
    return {
        "generated": tasks.now(),
        "root": str(paths.root()),  # task `report` paths are relative to it
        "days": days,
        "kill_switch": paths.kill_file().exists(),
        "counts": dict(Counter(r["status"] for r in task_rows)),
        "running": [r["id"] for r in task_rows if r["status"] in ("running", "verifying")],
        "pending_approvals": sum(len(r["pending"]) for r in task_rows),
        "tasks": task_rows,
        "guard": guard_summary(days),
        "usage": usage.summarize(days),
    }


def as_json(days: int) -> str:
    return json.dumps(snapshot(days), indent=2, ensure_ascii=False, default=list)


def render(days: int) -> str:
    s = snapshot(days)
    counts = ", ".join(f"{n} {k}" for k, n in sorted(s["counts"].items())) or "no tasks"
    v = s["guard"]["verdicts"]
    u = s["usage"]["total"]
    lines = [
        f"kill switch: {'ON' if s['kill_switch'] else 'off'}",
        f"tasks: {counts}",
        f"running: {', '.join(s['running']) or 'nothing'}",
        f"waiting for your approval: {s['pending_approvals']}",
        f"guard, last {s['days']} days: {v.get('allow', 0)} allowed, {v.get('deny', 0)} denied, "
        f"{v.get('ask', 0)} parked for approval",
        f"agents, last {s['days']} days: {u['calls']} calls, {u['tokens']:,} tokens, est ${u['cost_usd']:.2f}",
    ]
    for r in s["tasks"]:
        for p in r["pending"]:
            lines.append(f"  ⏸ {r['id']} {p['text']}")
    return "\n".join(lines)
