"""`sadyanx mcp`: a local MCP server (stdio) with five tools, for the desktop dashboard.

The dashboard runs inside the Claude desktop app, which can call MCP servers configured on
this Mac (`host:sadyanx`). That is a much narrower door than letting the page run programs:
these five tools are all it can reach.

  status   the snapshot `sadyanx status --json` prints (read-only)
  report   a task's report, runs/<task>/report.md (read-only)
  timeline a task's events, agent calls, approvals and denials: `sadyanx log <task> --json` (read-only)
  approve  `sadyanx approve <task> <approval>`: one named approval, never --all
  reject   `sadyanx reject <task> <approval> [--reason ...]`

Each call runs the CLI in a fresh process, so the CLI stays the only path to an approval (kill
switch, stale ids and all), and code changes apply without restarting the server. Arguments are
checked against their shape and passed as an argument list, never through a shell.

Agents must never reach these tools: a task's agent could approve its own parked action. The
guard denies them (guard.SADYANX_MCP_TOOL), and as a second lock this server refuses every call
when it was started from inside a sadyanx agent session (its environment says so).
"""

from __future__ import annotations

import json
import re
import subprocess
import sys

from . import paths

PROTOCOL_VERSION = "2025-06-18"
TASK_ID = re.compile(r"^[A-Za-z0-9_-]{1,16}$")
APPROVAL_ID = re.compile(r"^[0-9a-f]{8}$")
MAX_REASON = 300
MAX_REPORT = 256 * 1024
AGENT_ENV = ("SADYANX_GUARD_CTX", "SADYANX_TASK_ID")

_ID_PROPS = {
    "task": {"type": "string", "pattern": TASK_ID.pattern, "description": "Task id, e.g. 0005"},
    "approval": {"type": "string", "pattern": APPROVAL_ID.pattern,
                 "description": "The pending approval's id, as `status` lists it"},
}
TOOLS = [
    {
        "name": "status",
        "title": "sadyanx status",
        "description": "The harness at a glance: queue, approvals waiting, kill switch, guard verdicts and "
                       "token usage (the JSON `sadyanx status --json` prints). Changes nothing.",
        "inputSchema": {"type": "object", "properties": {
            "days": {"type": "integer", "minimum": 1, "maximum": 365, "default": 30,
                     "description": "Window for usage and guard numbers"}}, "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "report",
        "title": "A task's report",
        "description": "The Markdown report of one task (runs/<task>/report.md): what was done, the "
                       "acceptance criteria with their evidence, gates, reviews and CI. Changes nothing.",
        "inputSchema": {"type": "object", "properties": {"task": dict(_ID_PROPS["task"])}, "required": ["task"],
                        "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "timeline",
        "title": "A task's timeline",
        "description": "Everything that happened to one task, in order (`sadyanx log <task> --json`): the "
                       "runner's events, each agent call with its tokens and cost, approvals and how they were "
                       "decided, and what the guard denied. Changes nothing.",
        "inputSchema": {"type": "object", "properties": {"task": dict(_ID_PROPS["task"])}, "required": ["task"],
                        "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "approve",
        "title": "Approve one parked action",
        "description": "Approve exactly one pending approval of a task, then the harness carries it out and "
                       "resumes the task (`sadyanx approve <task> <approval>`). Refused while the kill switch "
                       "is on or if that approval is no longer pending.",
        "inputSchema": {"type": "object", "properties": dict(_ID_PROPS), "required": ["task", "approval"],
                        "additionalProperties": False},
        "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False,
                        "openWorldHint": True},
    },
    {
        "name": "reject",
        "title": "Reject one parked action",
        "description": "Reject exactly one pending approval of a task; the action does not run and the agent is "
                       "told why (`sadyanx reject <task> <approval> --reason ...`).",
        "inputSchema": {"type": "object", "properties": {
            **_ID_PROPS, "reason": {"type": "string", "maxLength": MAX_REASON,
                                    "description": "Why, for the agent (optional)"}},
            "required": ["task", "approval"], "additionalProperties": False},
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False,
                        "openWorldHint": True},
    },
]


class ToolError(Exception):
    pass


def _cli(args: list[str], timeout: int, run=subprocess.run) -> str:
    from .runner import agent_env  # late: keeps `sadyanx mcp` start-up light
    try:
        proc = run([sys.executable, str(paths.sadyanx_bin()), *args], capture_output=True, text=True,
                   timeout=timeout, stdin=subprocess.DEVNULL, env=agent_env(), cwd=str(paths.root()))
    except subprocess.TimeoutExpired:
        raise ToolError(f"sadyanx {args[0]} took longer than {timeout} s") from None
    if proc.returncode != 0:
        text = (proc.stderr or proc.stdout or "").strip().removeprefix("sadyanx: ")
        raise ToolError(text[-600:] or f"sadyanx {args[0]} failed (exit {proc.returncode})")
    return proc.stdout


def _report(task: str) -> str:
    """Only runs/<task>/report.md, and only if it really is that file (no link out of runs/)."""
    runs = paths.runs_dir().resolve()
    f = paths.run_dir(task) / "report.md"
    try:
        real = f.resolve(strict=True)
    except (FileNotFoundError, RuntimeError):
        raise ToolError(f"task {task} has no report yet") from None
    if real != runs / task / "report.md" or not real.is_file():
        raise ToolError(f"task {task} has no report it may show")
    data = real.read_bytes()
    text = data[:MAX_REPORT].decode("utf-8", errors="replace")
    if len(data) > MAX_REPORT:
        text += f"\n\n_(cut at {MAX_REPORT // 1024} KB; the whole report is in {real})_\n"
    return text


def call(name: str, arguments: dict, env: dict, run=subprocess.run) -> str:
    """Run one tool; returns its text result or raises ToolError."""
    if any(env.get(k) for k in AGENT_ENV):
        raise ToolError("sadyanx tools are not available inside a sadyanx agent session")
    args = arguments if isinstance(arguments, dict) else {}
    if name == "status":
        days = args.get("days", 30)
        if not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= 365:
            raise ToolError("days must be a whole number from 1 to 365")
        out = _cli(["status", "--json", "--days", str(days)], timeout=90, run=run)
        try:
            json.loads(out)  # it must be the snapshot, nothing else
        except json.JSONDecodeError:
            raise ToolError("sadyanx status did not print JSON: " + out.strip()[:200]) from None
        return out
    if name in ("report", "timeline"):
        if set(args) - {"task"}:
            raise ToolError("unexpected arguments: " + ", ".join(sorted(set(args) - {"task"})))
        task = args.get("task")
        if not isinstance(task, str) or not TASK_ID.fullmatch(task):
            raise ToolError("task must be a task id such as 0005")
        if name == "report":
            return _report(task)
        out = _cli(["log", task, "--json"], timeout=60, run=run)
        try:
            json.loads(out)
        except json.JSONDecodeError:
            raise ToolError("sadyanx log did not print JSON: " + out.strip()[:200]) from None
        return out
    if name in ("approve", "reject"):
        extra = set(args) - {"task", "approval"} - ({"reason"} if name == "reject" else set())
        if extra:
            raise ToolError("unexpected arguments: " + ", ".join(sorted(extra)))
        task, approval = args.get("task"), args.get("approval")
        if not isinstance(task, str) or not TASK_ID.fullmatch(task):
            raise ToolError("task must be a task id such as 0005")
        if not isinstance(approval, str) or not APPROVAL_ID.fullmatch(approval):
            raise ToolError("approval must be the 8-character id that status lists")
        cli_args = [name, task, approval]
        reason = args.get("reason", "")
        if name == "reject" and reason:
            if not isinstance(reason, str) or len(reason) > MAX_REASON:
                raise ToolError(f"reason must be text of at most {MAX_REASON} characters")
            cli_args += ["--reason", reason]
        # opening a PR or sending on Slack happens before the CLI returns
        return _cli(cli_args, timeout=600, run=run).strip() or f"{name}d"
    raise ToolError(f"unknown tool: {name}")


def handle(msg: dict, env: dict, run=subprocess.run) -> dict | None:
    """One JSON-RPC message in, its response out (None for notifications)."""
    method, mid = msg.get("method"), msg.get("id")
    if mid is None:
        return None  # notifications (initialized, cancelled...) need no answer
    params = msg.get("params") or {}
    if method == "initialize":
        result = {"protocolVersion": params.get("protocolVersion") or PROTOCOL_VERSION,
                  "capabilities": {"tools": {"listChanged": False}},
                  "serverInfo": {"name": "sadyanx", "version": "1"},
                  "instructions": "Local control of the sadyanx harness for its dashboard: read the status, a "
                                  "task's report or timeline, approve or reject one parked action."}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        try:
            text, error = call(str(params.get("name")), params.get("arguments") or {}, env, run), False
        except ToolError as exc:
            text, error = str(exc), True
        result = {"content": [{"type": "text", "text": text}], "isError": error}
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def serve(env: dict, stdin=None, stdout=None) -> None:
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            out = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        else:
            out = handle(msg, env) if isinstance(msg, dict) else \
                {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "invalid request"}}
        if out is not None:
            stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
            stdout.flush()
