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
import statistics
from collections import Counter, defaultdict

from . import approvals, paths, persona, runner, tasks, usage

TASK_FIELDS = ("title", "status", "type", "repo", "mode", "effort", "phase", "attempts", "cost_usd",
               "budget_usd", "jira", "pr_url", "doc_url", "report", "reason", "created", "updated")
# Spend worth a look: a capped run past this share of its budget, or a task costing this many
# times the median of finished tasks like it (same type and mode), with enough peers and dollars
# that the comparison means something.
NEAR_BUDGET = 0.8
PEER_FACTOR = 2.0
PEERS_MIN = 3
PEER_MIN_USD = 1.0
WEEK = dt.timedelta(days=7)
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


def audit_records(days: int) -> list[tuple[str, dict]]:
    """(task id, record) for every guard decision in the window, from runs/*/audit.jsonl."""
    since = (dt.datetime.now() - dt.timedelta(days=days)).isoformat()
    out = []
    for f in sorted(paths.runs_dir().glob("*/audit.jsonl")):
        for line in f.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict) and rec.get("ts", "") >= since:
                out.append((f.parent.name, rec))
    return out


def guard_summary(days: int) -> dict:
    """What the guard decided in the window: totals, categories of what it stopped, the latest denials."""
    verdicts: Counter = Counter()
    stopped: dict[str, Counter] = defaultdict(Counter)
    recent = []
    for task_id, rec in audit_records(days):
        verdict = rec.get("verdict") or "?"
        verdicts[verdict] += 1
        if verdict != "allow":
            stopped[verdict][rec.get("category") or "?"] += 1
            if verdict == "deny":
                recent.append({"ts": rec.get("ts", ""), "task": task_id, "tool": rec.get("tool", ""),
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


def run_kind(task_id: str) -> str:
    """How the task's last run was set up: supervised, autonomous, or "" before it first ran."""
    try:
        ctx = json.loads((paths.runs_dir() / task_id / "guard.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    return "supervised" if isinstance(ctx, dict) and ctx.get("interactive") else "autonomous"


def spend(rows: list[dict]) -> None:
    """Add to each task row its spend against its budget and against finished tasks like it.

    `budget_used` is cost / budget, or None for a supervised run: it has no dollar cap (you
    watching are the cap). `peer_median_usd` is the median cost of the other finished tasks of the
    same type and mode, when there are at least PEERS_MIN. `cost_flag` is "budget" (a capped run
    past NEAR_BUDGET of it), "peers" (PEER_FACTOR times that median and at least PEER_MIN_USD) or ""."""
    finished: dict[tuple, list[tuple[str, float]]] = defaultdict(list)
    for r in rows:
        if r["status"] in ("done", "failed") and r["cost_usd"] > 0:
            finished[(r["type"], r["mode"])].append((r["id"], r["cost_usd"]))
    for r in rows:
        capped = r["run"] != "supervised" and r["budget_usd"] > 0
        r["budget_used"] = round(r["cost_usd"] / r["budget_usd"], 3) if capped else None
        others = [c for tid, c in finished[(r["type"], r["mode"])] if tid != r["id"]]
        median = statistics.median(others) if len(others) >= PEERS_MIN else 0.0
        r["peer_median_usd"] = round(median, 2) if median else None
        r["peers"] = len(others) if median else 0
        if capped and r["cost_usd"] >= NEAR_BUDGET * r["budget_usd"]:
            r["cost_flag"] = "budget"
        elif median and r["cost_usd"] >= max(PEER_FACTOR * median, PEER_MIN_USD):
            r["cost_flag"] = "peers"
        else:
            r["cost_flag"] = ""


def weekly(recs: list[dict], rows: list[dict], audit: list[tuple[str, dict]], now: dt.datetime) -> dict:
    """The last 7 days against the 7 before, from the logs: agent tokens, tasks finished, first-pass
    rate, tokens per finished task, fix-round calls and guard denials. A task counts as finished in
    the week of its last update while done; its tokens and fix rounds are all of its runs'. A rate
    with nothing to divide by is None."""
    start, mid = (now - 2 * WEEK).isoformat(), (now - WEEK).isoformat()

    def week(ts: str) -> str | None:
        return None if not ts or ts < start else "previous" if ts < mid else "current"

    out = {w: {"tokens": 0, "finished": 0, "first_pass_pct": None, "tokens_per_finished": None,
               "fix_calls": 0, "denials": 0} for w in ("current", "previous")}
    task_tokens: Counter = Counter()
    task_fixed: set[str] = set()
    for r in recs:
        task_tokens[r.get("task", "")] += usage.total_tokens(r)
        if r.get("phase") == "fix":
            task_fixed.add(r.get("task", ""))
        w = week(str(r.get("ts", "")))
        if w:
            out[w]["tokens"] += usage.total_tokens(r)
            out[w]["fix_calls"] += r.get("phase") == "fix"
    first: Counter = Counter()
    spent: Counter = Counter()
    for t in rows:
        w = week(t.get("updated") or "") if t["status"] == "done" else None
        if w:
            out[w]["finished"] += 1
            first[w] += t["id"] not in task_fixed
            spent[w] += task_tokens[t["id"]]
    for _, rec in audit:
        w = week(rec.get("ts", ""))
        if w and rec.get("verdict") == "deny":
            out[w]["denials"] += 1
    for w, m in out.items():
        if m["finished"]:
            m["first_pass_pct"] = round(first[w] / m["finished"] * 100)
            m["tokens_per_finished"] = round(spent[w] / m["finished"])
    return out


def _task_row(t: tasks.Task) -> dict:
    row = {"id": t.id, **{k: t.get(k) for k in TASK_FIELDS}}
    row["cost_usd"] = t.num("cost_usd")
    row["budget_usd"] = t.num("budget_usd")
    row["run"] = run_kind(t.id)
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
    spend(task_rows)
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
        # all usage records: a task finished this week may have run long before
        "weekly": weekly(usage.records(3650), task_rows, audit_records(14), dt.datetime.now()),
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
    for r in s["tasks"]:
        if r["cost_flag"] == "budget":
            lines.append(f"  $ {r['id']} spent ${r['cost_usd']:.2f} of its ${r['budget_usd']:.2f} budget")
        elif r["cost_flag"] == "peers":
            lines.append(f"  $ {r['id']} spent ${r['cost_usd']:.2f}, {r['cost_usd'] / r['peer_median_usd']:.1f}x "
                         f"the median of {r['peers']} finished {r['mode']} {r['type']} tasks")
    return "\n".join(lines)
