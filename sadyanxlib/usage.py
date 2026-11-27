"""Where the tokens go.

`sadyanx usage`           numbers only, computed here (no model, no tokens).
`sadyanx usage --review`  hands those numbers and the config to an agent in balanced mode,
                          which proposes savings. It has no tools: it cannot change anything.
                          You apply what you approve (via /task usage review).

On a Max subscription the dollar figures are list-price estimates; what runs out is the
usage quota, so tokens are the primary measure.
"""

from __future__ import annotations

import datetime as dt
import json
import shutil
import subprocess
from collections import defaultdict
from pathlib import Path

from . import config, paths, tasks

REVIEW_DIR = "_review"


def _total(tok: dict) -> int:
    return sum(int(tok.get(k) or 0) for k in ("input", "output", "cache_read", "cache_write"))


def records(days: int) -> list[dict]:
    since = (dt.datetime.now() - dt.timedelta(days=days)).isoformat()
    out = []
    for f in sorted(paths.runs_dir().glob("*/usage.jsonl")):
        for line in f.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("ts", "") >= since:
                out.append(rec)
    return out


def _bucket() -> dict:
    return {"calls": 0, "tokens": 0, "output": 0, "cost_usd": 0.0, "turns": 0, "seconds": 0}


def _add(b: dict, r: dict) -> None:
    b["calls"] += 1
    b["tokens"] += _total(r.get("tokens", {}))
    b["output"] += int(r.get("tokens", {}).get("output") or 0)
    b["cost_usd"] = round(b["cost_usd"] + float(r.get("cost_usd") or 0), 4)
    b["turns"] += int(r.get("turns") or 0)
    b["seconds"] += int(r.get("seconds") or 0)


def summarize(days: int) -> dict:
    recs = records(days)
    meta = {t.id: t for t in tasks.all_tasks()}
    total = _bucket()
    cache = {"read": 0, "write": 0, "input": 0}
    by = {k: defaultdict(_bucket) for k in ("mode", "model", "effort", "model_effort", "phase", "repo", "type", "day")}
    per_task: dict[str, dict] = {}
    for r in recs:
        _add(total, r)
        tok = r.get("tokens", {})
        cache["read"] += int(tok.get("cache_read") or 0)
        cache["write"] += int(tok.get("cache_write") or 0)
        cache["input"] += int(tok.get("input") or 0)
        _add(by["mode"][r.get("mode") or "?"], r)
        _add(by["model"][r.get("model") or "?"], r)
        _add(by["effort"][r.get("effort") or "?"], r)
        _add(by["model_effort"][f"{r.get('model') or '?'}/{r.get('effort') or '?'}"], r)
        _add(by["phase"][r.get("phase") or "?"], r)
        _add(by["repo"][r.get("repo") or f"({r.get('type')})"], r)
        _add(by["type"][r.get("type") or "?"], r)
        _add(by["day"][str(r.get("ts", ""))[:10] or "?"], r)
        tid = r.get("task", "?")
        t = per_task.setdefault(tid, {**_bucket(), "id": tid, "modes": set(), "phases": set(), "max_attempt": 0})
        _add(t, r)
        t["modes"].add(r.get("mode"))
        t["phases"].add(r.get("phase"))
        t["max_attempt"] = max(t["max_attempt"], int(r.get("attempt") or 0))
    task_rows = []
    for tid, t in per_task.items():
        task = meta.get(tid)
        t["title"] = task.title if task else ("usage review" if tid == REVIEW_DIR else "")
        t["status"] = task.status if task else ""
        t["task_mode"] = task.get("mode") if task else ""
        t["fix_rounds"] = "fix" in t["phases"]
        t["first_pass"] = bool(task and task.status == "done" and "fix" not in t["phases"])
        t["modes"] = sorted(m for m in t["modes"] if m)
        t["phases"] = sorted(p for p in t["phases"] if p)
        task_rows.append(t)
    task_rows.sort(key=lambda x: x["tokens"], reverse=True)
    seen = cache["read"] + cache["write"] + cache["input"]
    return {
        "days": days,
        "total": total,
        "cache_hit_ratio": round(cache["read"] / seen, 3) if seen else 0.0,
        "by_mode": dict(by["mode"]),
        "by_model": dict(by["model"]),
        "by_effort": dict(by["effort"]),
        "by_model_effort": dict(by["model_effort"]),
        "by_phase": dict(by["phase"]),
        "by_repo": dict(by["repo"]),
        "by_type": dict(by["type"]),
        "by_day": dict(sorted(by["day"].items())),
        "tasks": task_rows,
    }


def as_json(days: int) -> str:
    return json.dumps(summarize(days), indent=2, ensure_ascii=False, default=list)


def _fmt(n: int) -> str:
    return f"{n / 1e6:.2f}M" if n >= 1e6 else f"{n / 1e3:.1f}k" if n >= 1e3 else str(n)


def _table(title: str, rows: dict, total_tokens: int) -> list[str]:
    lines = [f"\n{title}", f"  {'':<18} {'calls':>5} {'tokens':>9} {'share':>6} {'turns':>6} {'est $':>8}"]
    for key, b in sorted(rows.items(), key=lambda kv: kv[1]["tokens"], reverse=True):
        share = b["tokens"] / total_tokens * 100 if total_tokens else 0
        lines.append(f"  {key[:18]:<18} {b['calls']:>5} {_fmt(b['tokens']):>9} {share:>5.0f}% {b['turns']:>6} "
                     f"{b['cost_usd']:>8.2f}")
    return lines


def render(days: int) -> str:
    s = summarize(days)
    t = s["total"]
    if not t["calls"]:
        return f"no agent runs in the last {days} days"
    lines = [
        f"usage, last {days} days — {t['calls']} agent calls, {_fmt(t['tokens'])} tokens "
        f"({_fmt(t['output'])} output), {t['turns']} turns, cache hit {s['cache_hit_ratio'] * 100:.0f}%",
        "  (est $ is list price; on a Max plan what runs out is the usage quota)",
    ]
    lines += _table("by mode", s["by_mode"], t["tokens"])
    lines += _table("by model / effort", s["by_model_effort"], t["tokens"])
    lines += _table("by phase  (work = first try, fix = after failed gates)", s["by_phase"], t["tokens"])
    lines += _table("by repo", s["by_repo"], t["tokens"])
    lines.append("\ntop tasks")
    lines.append(f"  {'id':<6} {'status':<15} {'modes':<16} {'calls':>5} {'tokens':>9} {'1st pass':>8}  title")
    for row in s["tasks"][:10]:
        lines.append(f"  {row['id']:<6} {row['status'][:15]:<15} {','.join(row['modes'])[:16]:<16} {row['calls']:>5} "
                     f"{_fmt(row['tokens']):>9} {'yes' if row['first_pass'] else 'no':>8}  {row['title'][:50]}")
    lines.append("\nfor savings proposals: sadyanx usage --review")
    return "\n".join(lines)


def review(cfg: config.Config, days: int) -> str:
    data = summarize(days)
    if not data["total"]["calls"]:
        return f"no agent runs in the last {days} days: nothing to review"
    cfg_text = "\n\n".join(
        f"# {p.name}\n{p.read_text(encoding='utf-8')}" for p in
        (paths.config_dir() / "sadyanx.toml", paths.config_dir() / "repos.toml") if p.exists())
    template = (paths.prompts_dir() / "usage-review.md").read_text(encoding="utf-8")
    prompt = template.replace("$days", str(days)).replace(
        "$usage", json.dumps(data, indent=2, ensure_ascii=False, default=list)).replace("$config", cfg_text)
    mode = cfg.mode("balanced")
    from .runner import agent_env  # late import: runner imports this package's siblings
    workdir = paths.state_dir() / "review"
    workdir.mkdir(parents=True, exist_ok=True)
    claude = shutil.which(cfg.defaults["claude_bin"]) or str(Path.home() / ".local/bin/claude")
    args = [claude, "-p", prompt, "--model", mode.model, "--effort", mode.effort, "--tools", "",
            "--strict-mcp-config", "--exclude-dynamic-system-prompt-sections", "--no-session-persistence",
            "--output-format", "json", "--max-budget-usd", "1"]
    proc = subprocess.run(args, cwd=workdir, capture_output=True, text=True, timeout=600,
                          stdin=subprocess.DEVNULL, env=agent_env())
    try:
        result = json.loads(proc.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return f"the review agent failed (exit {proc.returncode}): {proc.stderr.strip()[:500]}"
    text = result.get("result", "").strip()
    out_dir = paths.run_dir(REVIEW_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = out_dir / f"review-{stamp}.md"
    path.write_text(text + "\n", encoding="utf-8")
    u = result.get("usage") or {}
    rec = {"ts": tasks.now(), "task": REVIEW_DIR, "type": "review", "repo": "", "phase": "review", "attempt": 1,
           "mode": mode.name, "model": mode.model, "effort": mode.effort, "outcome": "done",
           "turns": int(result.get("num_turns") or 0), "seconds": int((result.get("duration_ms") or 0) / 1000),
           "cost_usd": round(float(result.get("total_cost_usd") or 0), 4),
           "tokens": {"input": int(u.get("input_tokens") or 0), "output": int(u.get("output_tokens") or 0),
                      "cache_read": int(u.get("cache_read_input_tokens") or 0),
                      "cache_write": int(u.get("cache_creation_input_tokens") or 0)}}
    with (out_dir / "usage.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")
    return f"{text}\n\n(saved to {path}; nothing was changed)"
