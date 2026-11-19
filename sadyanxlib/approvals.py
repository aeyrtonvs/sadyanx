"""Pending approvals for irreversible actions, stored per task in runs/<id>/approvals.json.

Two kinds:
  tool     an agent tool call the guard intercepted (a deploy, a comment, an MCP write).
           Approving it lets that exact call through once, then the agent session is
           resumed so it repeats the call.
  harness  an action the harness itself performs after approval (opening a PR on a
           work repo). The agent never holds the credentials for those.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import re
from pathlib import Path

from .tasks import now

FILENAME = "approvals.json"


def _norm_cmd(cmd: str) -> str:
    return re.sub(r"\s+", " ", cmd).strip()


def call_key(tool: str, tool_input: dict) -> str:
    if tool == "Bash":
        return "Bash:" + _norm_cmd(str(tool_input.get("command", "")))
    payload = {k: v for k, v in (tool_input or {}).items() if k != "description"}
    return tool + ":" + json.dumps(payload, sort_keys=True, ensure_ascii=False)


def approval_id(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:8]


@contextlib.contextmanager
def _locked(run_dir: Path):
    run_dir.mkdir(parents=True, exist_ok=True)
    with (run_dir / "approvals.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def load(run_dir: Path) -> list[dict]:
    f = run_dir / FILENAME
    if not f.exists():
        return []
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []


def _save(run_dir: Path, items: list[dict]) -> None:
    tmp = run_dir / (FILENAME + ".tmp")
    tmp.write_text(json.dumps(items, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(run_dir / FILENAME)


def request_tool(run_dir: Path, tool: str, tool_input: dict, category: str, reason: str) -> dict:
    """Record (or find) the pending approval for an intercepted tool call."""
    key = call_key(tool, tool_input)
    aid = approval_id(key)
    with _locked(run_dir):
        items = load(run_dir)
        for item in items:
            if item["id"] == aid and item["status"] in ("pending", "rejected"):
                return item
        record = {
            "id": aid, "kind": "tool", "tool": tool, "input": tool_input, "key": key,
            "category": category, "reason": reason, "status": "pending", "created": now(),
        }
        items = [i for i in items if i["id"] != aid] + [record]
        _save(run_dir, items)
        return record


def request_harness(run_dir: Path, action: str, payload: dict, category: str, reason: str) -> dict:
    key = f"harness:{action}:" + json.dumps(payload, sort_keys=True, ensure_ascii=False)
    aid = approval_id(key)
    with _locked(run_dir):
        items = [i for i in load(run_dir) if i["id"] != aid]
        record = {
            "id": aid, "kind": "harness", "action": action, "payload": payload, "key": key,
            "category": category, "reason": reason, "status": "pending", "created": now(),
        }
        _save(run_dir, items + [record])
        return record


def consume_if_approved(run_dir: Path, tool: str, tool_input: dict) -> bool:
    """True once for an approved call; the grant is then spent."""
    key = call_key(tool, tool_input)
    with _locked(run_dir):
        items = load(run_dir)
        for item in items:
            if item.get("key") == key and item["status"] == "approved":
                item["status"] = "consumed"
                item["consumed"] = now()
                _save(run_dir, items)
                return True
    return False


def find_rejected(run_dir: Path, tool: str, tool_input: dict) -> dict | None:
    key = call_key(tool, tool_input)
    for item in load(run_dir):
        if item.get("key") == key and item["status"] == "rejected":
            return item
    return None


def pending(run_dir: Path) -> list[dict]:
    return [i for i in load(run_dir) if i["status"] == "pending"]


def decide(run_dir: Path, ids: list[str] | None, status: str, note: str = "") -> list[dict]:
    """Approve or reject pending items. ids=None means every pending item."""
    assert status in ("approved", "rejected")
    changed = []
    with _locked(run_dir):
        items = load(run_dir)
        for item in items:
            if item["status"] != "pending":
                continue
            if ids is None or item["id"] in ids:
                item["status"] = status
                item["decided"] = now()
                if note:
                    item["note"] = note
                changed.append(item)
        _save(run_dir, items)
    if ids:
        missing = set(ids) - {c["id"] for c in changed}
        if missing:
            raise LookupError("no pending approval with id: " + ", ".join(sorted(missing)))
    return changed


def mark(run_dir: Path, aid: str, status: str, **extra) -> None:
    with _locked(run_dir):
        items = load(run_dir)
        for item in items:
            if item["id"] == aid:
                item["status"] = status
                item.update(extra)
        _save(run_dir, items)


def describe(item: dict) -> str:
    if item["kind"] == "harness":
        what = item.get("action", "")
        title = (item.get("payload") or {}).get("title", "")
        return f"[{item['id']}] {item['category']}: {what} — {title}"
    if item["tool"] == "Bash":
        what = _norm_cmd(str(item["input"].get("command", "")))
    else:
        what = item["tool"] + " " + json.dumps(item["input"], ensure_ascii=False)
    if len(what) > 200:
        what = what[:197] + "..."
    return f"[{item['id']}] {item['category']}: {what}"
