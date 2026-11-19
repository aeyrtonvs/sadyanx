"""The PreToolUse guard: every tool call an agent makes passes through `decide`.

Three verdicts:
  allow  the call runs.
  deny   forbidden outright (pushing, reading secrets, writing outside the worktree...).
  gate   irreversible (deploy, merge, talking to third parties, destructive change): the
         call is parked as a pending approval and denied for now; once you approve it,
         the same call goes through exactly once. In a supervised run (`interactive` in the
         context) the call is not parked: Claude Code asks the human on the spot ("ask").
         In a helper session (`deny_gated`: the code review) it is just denied.

How Bash is read: the command is split into simple commands (on ; && || | newlines and
command substitutions), env assignments and wrappers (env, npx, timeout...) are peeled
off, and each rule must match *at the command position*. So `firebase deploy` is gated
but `grep -r "firebase deploy" docs/` is not. Paths named by write commands and
redirections are resolved (following `cd`) and must stay inside the worktree.

Honest limits: this catches what an agent does by accident or habit, not a determined
attempt to hide a write inside `python -c`. The outer layers are the per-task worktree,
the budget, the kill switch and the audit log.

Fails closed: an exception in the guard denies the call.

Repeated calls: the Nth identical call in a row (`max_identical_calls`, default 3) is denied
with a nudge to change approach. It is a cheap doom-loop cap, not a safety rule, so it never
blocks a call when its counter cannot be read; the budget remains the hard stop.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import approvals
from .tasks import now

ALLOW, DENY, GATE = "allow", "deny", "gate"


@dataclass
class Ctx:
    task_id: str
    run_dir: Path
    write_roots: list[str]
    repo_kind: str = "personal"
    protected_branches: list[str] = field(default_factory=list)
    extra_forbidden: list[str] = field(default_factory=list)
    extra_gated: list[str] = field(default_factory=list)
    mcp_allow: list[str] = field(default_factory=list)
    kill_file: str = ""
    max_identical_calls: int = 3  # consecutive identical calls before the guard steps in; 0 = off
    interactive: bool = False  # supervised run: a human watches the session and answers prompts
    deny_gated: bool = False   # a helper session (the code review): approvable actions are simply denied
    home: str = str(Path.home())

    @classmethod
    def load(cls, path: str) -> "Ctx":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        data["run_dir"] = Path(data["run_dir"])
        return cls(**data)


@dataclass
class Verdict:
    kind: str
    category: str = ""
    reason: str = ""


# --------------------------------------------------------------------------
# Rules. Each regex is matched with re.match against one simple command, already
# stripped of env assignments and wrappers, so it must describe the command itself.
# --------------------------------------------------------------------------

_GIT = r"git(?:\s+-[Cc]\s+\S+|\s+--[\w-]+(?:=\S+)?)*\s+"
_ANY = r"[^\n]*"  # rest of a simple command

FORBIDDEN_CMD: list[tuple[str, str]] = [
    (r"(?:sudo|doas|su)\b", "sudo/su are not allowed"),
    (_GIT + r"push\b", "agents do not push: the harness pushes the branch at the end, after verification"),
    (_GIT + r"remote\s+(?:add|set-url|remove|rm|rename)\b", "git remotes may not be changed"),
    (_GIT + r"config\s+(?:--global|--system)\b", "global git config may not be changed"),
    (_GIT + r"worktree\s+(?:add|remove|prune|move|lock|unlock)\b", "worktrees are managed by the harness"),
    (_GIT + r"credential\b", "git credentials may not be read"),
    (r"(?:crontab|launchctl|systemctl)\b", "no scheduling of system jobs or services"),
    (r"defaults\s+(?:write|delete)\b", "macOS settings may not be changed"),
    (r"security\s+(?:find-|dump-|export|unlock|add-)", "the keychain may not be used"),
    (r"gh\s+auth\s+(?:token|login|logout|refresh|setup-git|switch)\b", "gh credentials may not be read or changed"),
    (r"gh\s+auth\s+status\b" + _ANY + r"(?:-t|--show-token)\b", "gh credentials may not be read"),
    (r"aws\s+(?:configure\s+(?:get|export-credentials|set)|sts\s+get-session-token|ecr\s+get-login-password"
     r"|codeartifact\s+(?:get-authorization-token|login))\b", "AWS credentials may not be read or changed"),
    (r"printenv\s+\S*(?:TOKEN|KEY|SECRET|PASS)", "secret environment variables may not be read"),
    (r"(?:pkill|killall)\b", "pkill/killall can kill unrelated processes; use kill <pid>"),
    (r"brew\s+(?:install|uninstall|upgrade|reinstall|tap|untap|services|link|unlink)\b",
     "no global software installs on this machine"),
    (r"(?:npm|pnpm|yarn|bun)\s+(?:i|install|add|remove|uninstall|rm)\b" + _ANY + r"\s(?:-g|--global)\b",
     "no global package installs"),
    (r"(?:uv\s+tool|pipx)\s+(?:install|uninstall|upgrade)\b", "no global tool installs"),
    (r"gh\s+pr\s+create\b", "the harness opens the PR after verification; leave your commits on the branch"),
    (r"claude\b(?!" + _ANY + r"--version)", "no launching other Claude sessions or changing Claude's config"),
]

GATED_CMD: list[tuple[str, str]] = [
    # deploys and changes to real environments
    (r"firebase\b" + _ANY + r"\s(?:deploy|hosting:channel:deploy|functions:delete|firestore:delete"
     r"|database:(?:set|update|remove|push)|auth:import)\b", "deploy"),
    (r"(?:npm|pnpm|yarn|bun)\s+publish\b", "deploy"),
    (r"(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?(?:deploy|release|publish)[\w:-]*\b", "deploy"),
    (r"(?:uv|poetry)\s+publish\b|twine\s+upload\b", "deploy"),
    (r"(?:terraform|tofu|terragrunt)\b" + _ANY + r"\s(?:apply|destroy|import|taint|untaint"
     r"|state\s+(?:rm|mv|push|replace-provider))\b", "deploy"),
    (r"pulumi\s+(?:up|destroy|refresh|import)\b", "deploy"),
    (r"kubectl\b" + _ANY + r"\s(?:apply|create|delete|patch|replace|scale|rollout|drain|cordon|uncordon|edit"
     r"|set|label|annotate|taint|exec|cp|run|expose|autoscale)\b", "deploy"),
    (r"helm\b" + _ANY + r"\s(?:install|upgrade|uninstall|rollback|delete)\b", "deploy"),
    (r"docker\s+(?:push\b|buildx\s+build\b" + _ANY + r"--push\b)", "deploy"),
    (r"gh\s+(?:release\s+(?:create|edit|delete|upload)|workflow\s+(?:run|enable|disable)"
     r"|run\s+(?:rerun|cancel))\b", "deploy"),
    (r"(?:vercel|netlify)\b" + _ANY + r"\b(?:deploy|--prod)\b", "deploy"),
    (r"(?:fly|flyctl)\s+deploy\b|eas\s+(?:submit|update)\b", "deploy"),
    (r"make\b" + _ANY + r"\b(?:deploy|apply|release|publish|migrate)\b", "deploy"),
    (r"gcloud\b" + _ANY + r"\b(?:deploy|create|delete|update|set-iam-policy|add-iam-policy-binding|apply)\b", "deploy"),
    (r"aws(?>(?:\s+--\S+(?:\s+(?!-)\S+)?)*)\s+[\w-]+\s+(?!(?:describe|list|get|head|wait|help|ls|filter|lookup"
     r"|search|batch-get|query|scan|tail|validate|estimate|simulate|test|presign)[\w-]*\b)[a-z]", "deploy"),
    # merge through a code host
    (r"gh\s+pr\s+(?:merge|ready)\b", "merge"),
    # talking to third parties
    (r"gh\s+(?:pr|issue)\s+(?:comment|review|edit|close|reopen|lock|unlock|create|transfer|delete)\b", "third-party"),
    (r"gh\s+api\b(?=" + _ANY + r"(?:\s-X\s*|\s--method[\s=]*)(?:POST|PUT|PATCH|DELETE))", "third-party"),
    (r"gh\s+api\b" + _ANY + r"\s(?:-f|-F|--field|--raw-field|--input)\b", "third-party"),
    (r"(?:mail|sendmail|mutt|osascript)\b", "third-party"),
    # destructive changes
    (_GIT + r"branch\b" + _ANY + r"\s(?:-D|-d|--delete)\b", "destructive"),
    (_GIT + r"tag\b" + _ANY + r"\s(?:-d|--delete)\b", "destructive"),
    (_GIT + r"(?:filter-branch|filter-repo)\b", "destructive"),
    (r"prisma\s+(?:migrate\s+(?:deploy|reset|resolve)|db\s+(?:push|execute))\b", "destructive"),
    (r"alembic\s+(?:upgrade|downgrade|stamp)\b", "destructive"),
    (r"(?:knex|sequelize|sequelize-cli|typeorm|drizzle-kit|flyway|liquibase)\b" + _ANY + r"(?:migrat|push|drop)",
     "destructive"),
    (r"(?:rails|rake)\s+db:(?:migrate|drop|reset|rollback|schema:load|seed)\b", "destructive"),
    (r"(?:python3?\s+)?manage\.py\s+(?:migrate|flush|loaddata)\b", "destructive"),
    (r"(?:mysql|psql|mongosh|mongo|redis-cli|sqlcmd|sqlite3)\b", "destructive"),
]

# Checked against the whole command string (they span simple commands or quoting).
FORBIDDEN_WHOLE: list[tuple[str, str]] = [
    (r"\b(?:curl|wget)\b[^\n]*\|\s*(?:sudo\s+)?(?:ba|z|da|k)?sh\b", "downloaded scripts may not be executed"),
    (r"\$\{?[A-Za-z_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY|PRIVATE_KEY)[A-Za-z0-9_]*\}?",
     "secret environment variables may not be used"),
]

SQL_DESTRUCTIVE = re.compile(r"\b(?:drop\s+(?:table|database|schema)|truncate\s+table)\b", re.I)

# Requests that write to a remote host are "talking to third parties"; localhost is fine.
_CURL_WRITE = re.compile(
    r"(?:curl\b(?=" + _ANY + r"(?:\s-X\s*|\s--request[\s=]*)(?:POST|PUT|PATCH|DELETE))"
    r"|curl\b" + _ANY + r"\s(?:-d|--data[\w-]*|-F|--form|--json|-T|--upload-file)\b"
    r"|wget\b" + _ANY + r"\s--(?:post-data|post-file|method|body-data))"
)
_REMOTE_URL = re.compile(r"https?://(?!localhost\b|127\.0\.0\.1\b|0\.0\.0\.0\b|\[::1\])")

SECRET_PATH = re.compile(
    r"(?:^|/)\.env(?:\.(?!(?:example|sample|template|dist|defaults)\b)[\w.-]+)?$"
    r"|\.(?:pem|key|p12|pfx|keystore|jks|tfstate|tfstate\.backup)$"
    r"|(?:^|/)id_(?:rsa|ed25519|ecdsa|dsa)$"
    r"|(?:^|/)\.ssh(?:/|$)"
    r"|(?:^|/)\.aws/(?:credentials|config)$"
    r"|(?:^|/)\.(?:netrc|npmrc|pypirc|pgpass|git-credentials)$"
    r"|(?:^|/)\.docker/config\.json$"
    r"|(?:^|/)\.kube/config$"
    r"|(?:^|/)\.config/gh/hosts\.yml$"
    r"|(?:^|/)\.claude/settings(?:\.local)?\.json$"
    r"|(?:^|/)\.claude\.json$"
    r"|(?:^|/)\.claude/\.credentials\.json$"
    r"|(?:^|/)(?:credentials|secrets?|service-?account[\w-]*)\.(?:json|ya?ml|toml)$"
)

FORBIDDEN_TOOLS = {
    "CronCreate": "agents do not schedule jobs",
    "CronDelete": "agents do not schedule jobs",
    "ScheduleWakeup": "agents do not schedule jobs",
    "RemoteTrigger": "agents do not launch remote sessions",
    "EnterWorktree": "worktrees are managed by the harness",
    "ExitWorktree": "worktrees are managed by the harness",
    "PublishPlugin": "agents do not publish plugins",
}

WRITE_EDIT_TOOLS = {"Edit": "file_path", "Write": "file_path", "MultiEdit": "file_path", "NotebookEdit": "notebook_path"}

MCP_WRITE_OP = re.compile(
    r"^(?:add|create|edit|update|delete|remove|send|post|publish|transition|reply|comment|upload|share|invite"
    r"|merge|approve|assign|move|archive|set|write|put|patch|close|reopen|submit|batch|draft|schedule|cancel"
    r"|resolve|link|unlink|import|execute|run|trigger|deploy|certify|uncertify|unshare)",
    re.I,
)
MCP_DESTRUCTIVE_OP = re.compile(r"^(?:delete|remove|archive|unshare|uncertify)", re.I)

DEFAULT_MCP_ALLOW = [
    # moving a ticket's status is autonomous; commenting is not
    r"mcp__claude_ai_Atlassian__transitionJiraIssue",
    # your own private docs (research reports)
    r"mcp__claude_ai_Claude_Docs__(?:create|batch|update|guide|read|query|export)",
    r"mcp__plugin_context7_context7__.*",
]

HEREDOC = re.compile(r"<<-?[ \t]*(['\"]?)(\w+)\1([^\n]*)\n(.*?)\n[ \t]*\2[ \t]*(?=\n|$|\))", re.S)
REDIRECT = re.compile(r"(?<![<\d&>])\d?>>?\|?\s*([^\s;&|<>()]+)")
WRITES_ALL_ARGS = {"rm", "rmdir", "unlink", "shred", "srm", "mv", "touch", "mkdir", "chmod", "chown", "chgrp",
                   "truncate", "tee"}
WRITES_LAST_ARG = {"cp", "rsync", "ln", "install", "ditto"}
WRAPPERS = {"command", "nohup", "time", "nice", "exec", "builtin", "caffeinate", "npx", "bunx", "uvx", "xargs"}
SHELLS = {"bash", "sh", "zsh", "dash", "ksh"}


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _expand(token: str, ctx: Ctx) -> str | None:
    t = token.strip().strip("'\"")
    t = re.sub(r"^~(?=/|$)", ctx.home, t)
    t = t.replace("${HOME}", ctx.home).replace("$HOME", ctx.home)
    tmp = os.environ.get("TMPDIR", "/tmp")
    t = t.replace("${TMPDIR}", tmp).replace("$TMPDIR", tmp)
    t = t.replace("${PWD}", ".").replace("$PWD", ".")
    if "$" in t:
        return None  # unresolvable variable
    return t


def _resolve(path: str, cwd: str) -> str:
    p = path if os.path.isabs(path) else os.path.join(cwd, path)
    return os.path.realpath(os.path.normpath(p))


def _inside(path: str, roots: list[str]) -> bool:
    for root in roots:
        r = os.path.realpath(root)
        if path == r or path.startswith(r.rstrip("/") + "/"):
            return True
    return False


def _tokens(segment: str) -> list[str]:
    try:
        return shlex.split(segment, posix=True)
    except ValueError:
        return segment.split()


def _strip_prefix(tokens: list[str]) -> list[str]:
    """Peel env assignments and wrappers: `FOO=1 npx timeout 30 firebase deploy` -> firebase deploy."""
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        base = os.path.basename(tok)
        if re.match(r"^[A-Za-z_]\w*=", tok) or base in WRAPPERS:
            i += 1
            while base in ("npx", "bunx", "uvx", "xargs") and i < len(tokens) and tokens[i].startswith("-"):
                i += 1
            continue
        if base == "env" and i + 1 < len(tokens):
            i += 1
            while i < len(tokens) and (tokens[i].startswith("-") or "=" in tokens[i]):
                i += 1
            continue
        if base in ("timeout", "gtimeout"):
            i += 1
            while i < len(tokens) and tokens[i].startswith("-"):
                i += 1
            i += 1  # the duration
            continue
        if base in ("pnpm", "yarn") and i + 1 < len(tokens) and tokens[i + 1] in ("dlx", "exec"):
            i += 2
            continue
        break
    return tokens[i:]


def _current_branch(cwd: str) -> str:
    try:
        out = subprocess.run(["git", "-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"],
                             capture_output=True, text=True, timeout=5)
        return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _write_targets(name: str, args: list[str]) -> list[str]:
    positional = [a for a in args if not a.startswith("-")]
    if name in WRITES_ALL_ARGS:
        if name in ("chmod", "chown", "chgrp"):
            return positional[1:]
        return positional
    if name in WRITES_LAST_ARG:
        return positional[-1:]
    if name in ("sed", "gsed") and any(a.startswith("-i") or a == "--in-place" for a in args):
        return positional[1:]
    if name == "find" and any(a in ("-delete", "-exec", "-execdir", "-ok") for a in args):
        first_opt = next((i for i, a in enumerate(args) if a.startswith("-")), len(args))
        return args[:first_opt] or ["."]
    if name == "dd":
        return [a[3:] for a in args if a.startswith("of=")]
    return []


# --------------------------------------------------------------------------
# Decisions
# --------------------------------------------------------------------------

def _check_segment(seg: str, cwd: str, ctx: Ctx, depth: int) -> tuple[Verdict | None, str]:
    """Returns (verdict or None, cwd after this segment)."""
    for target in REDIRECT.findall(seg):
        if target.startswith("/dev/") or target.startswith("&"):
            continue
        exp = _expand(target, ctx)
        if exp is None:
            return Verdict(DENY, "write", f"redirect to a path with an unresolved variable: {target}"), cwd
        if not _inside(_resolve(exp, cwd), ctx.write_roots):
            return Verdict(DENY, "write", f"no writing outside the worktree: {target}"), cwd

    raw = _tokens(seg)
    if raw in (["env"], ["printenv"], ["set"], ["export"], ["export", "-p"], ["declare", "-x"]):
        return Verdict(DENY, "secrets", "the environment may not be dumped (it can hold secrets)"), cwd
    toks = _strip_prefix(raw)
    if not toks:
        return None, cwd
    name = os.path.basename(toks[0])
    args = toks[1:]
    norm = " ".join([name] + args)

    for tok in args:
        exp = _expand(tok, ctx) or tok
        if SECRET_PATH.search(exp):
            return Verdict(DENY, "secrets", f"secret files may not be read or copied: {tok}"), cwd

    if name == "cd":
        exp = _expand(args[0], ctx) if args else ctx.home
        return None, (_resolve(exp, cwd) if exp else cwd)

    if name in SHELLS and "-c" in args:
        idx = args.index("-c") + 1
        if idx < len(args) and depth < 3:
            return _check_bash(args[idx], cwd, ctx, depth + 1), cwd
    if name == "eval" and depth < 3:
        return _check_bash(" ".join(args), cwd, ctx, depth + 1), cwd

    for pattern in ctx.extra_forbidden:
        if re.search(pattern, norm):
            return Verdict(DENY, "forbidden", f"blocked by config rule: {pattern}"), cwd
    for pattern, reason in FORBIDDEN_CMD:
        if re.match(pattern, norm):
            return Verdict(DENY, "forbidden", reason), cwd

    for t in _write_targets(name, args):
        exp = _expand(t, ctx)
        if exp is None:
            return Verdict(DENY, "write",
                           f"{name} with an unresolved variable path ({t}); use literal paths"), cwd
        if not _inside(_resolve(exp, cwd), ctx.write_roots):
            return Verdict(DENY, "write", f"{name} outside the worktree is not allowed: {t}"), cwd

    for pattern in ctx.extra_gated:
        if re.search(pattern, norm):
            return Verdict(GATE, "custom", f"needs approval per config rule: {pattern}"), cwd
    for pattern, category in GATED_CMD:
        if re.match(pattern, norm):
            return Verdict(GATE, category, "irreversible action"), cwd
    if _CURL_WRITE.match(norm) and _REMOTE_URL.search(norm):
        return Verdict(GATE, "third-party", "request that writes to an external service"), cwd
    if re.match(_GIT + r"merge\b", norm) and _current_branch(cwd) in ctx.protected_branches:
        return Verdict(GATE, "merge", "merge into a protected branch"), cwd
    return None, cwd


def _strip_heredocs(cmd: str) -> tuple[str, list[str]]:
    """Drop heredoc bodies (commit messages, file contents) but return the ones fed to a
    shell, which are commands and must be checked."""
    shell_bodies: list[str] = []

    def repl(m: re.Match) -> str:
        line_start = cmd.rfind("\n", 0, m.start()) + 1
        before = cmd[line_start:m.start()]
        first = _strip_prefix(_tokens(before.strip()))[:1]
        if first and os.path.basename(first[0]) in SHELLS:
            shell_bodies.append(m.group(4))
        return "<<" + m.group(2) + m.group(3)

    return HEREDOC.sub(repl, cmd), shell_bodies


def _substitutions(cmd: str) -> list[str]:
    """Contents of $(...) and `...` outside single quotes, outermost level."""
    found, i, quote = [], 0, None
    while i < len(cmd):
        c = cmd[i]
        if quote == "'":
            if c == "'":
                quote = None
        elif c == "'" and quote is None:
            quote = "'"
        elif c == '"':
            quote = None if quote == '"' else '"'
        elif c == "\\":
            i += 1
        elif cmd.startswith("$(", i) and not cmd.startswith("$((", i):
            depth, j = 1, i + 2
            while j < len(cmd) and depth:
                depth += {"(": 1, ")": -1}.get(cmd[j], 0)
                j += 1
            found.append(cmd[i + 2:j - 1])
            i = j
            continue
        elif c == "`":
            j = cmd.find("`", i + 1)
            if j == -1:
                break
            found.append(cmd[i + 1:j])
            i = j + 1
            continue
        i += 1
    return found


def _split(cmd: str) -> list[str]:
    """Split into simple commands on ; && || | & newlines and ( ), outside quotes.
    Command substitutions are skipped here and checked separately."""
    segs, buf, quote, i = [], [], None, 0

    def flush():
        s = "".join(buf).strip()
        if s:
            segs.append(s)
        buf.clear()

    while i < len(cmd):
        c = cmd[i]
        if quote:
            buf.append(c)
            if c == quote:
                quote = None
            elif c == "\\" and quote == '"' and i + 1 < len(cmd):
                buf.append(cmd[i + 1])
                i += 1
        elif c in "'\"":
            quote = c
            buf.append(c)
        elif c == "\\" and i + 1 < len(cmd):
            buf.append(cmd[i + 1] if cmd[i + 1] != "\n" else " ")
            i += 1
        elif cmd.startswith("$(", i):
            depth, j = 1, i + 2
            while j < len(cmd) and depth:
                depth += {"(": 1, ")": -1}.get(cmd[j], 0)
                j += 1
            buf.append("__SUBST__")
            i = j
            continue
        elif c in ";|&\n()`":
            # keep fd duplications like 2>&1 and &> together
            if c == "&" and ((buf and buf[-1] == ">") or cmd.startswith("&>", i)):
                buf.append(c)
            else:
                flush()
        else:
            buf.append(c)
        i += 1
    flush()
    return segs


def _check_bash(cmd: str, cwd: str, ctx: Ctx, depth: int = 0) -> Verdict:
    cmd, shell_bodies = _strip_heredocs(cmd)
    for pattern, reason in FORBIDDEN_WHOLE:
        if re.search(pattern, cmd):
            return Verdict(DENY, "forbidden", reason)
    eff_cwd = cwd
    gated: Verdict | None = None
    nested = []
    if depth < 3:
        nested = [_check_bash(sub, cwd, ctx, depth + 1) for sub in _substitutions(cmd) + shell_bodies]
    for v in nested:
        if v.kind == DENY:
            return v
        if v.kind == GATE:
            gated = gated or v
    for seg in _split(cmd):
        verdict, eff_cwd = _check_segment(seg, eff_cwd, ctx, depth)
        if verdict is None:
            continue
        if verdict.kind == DENY:
            return verdict  # a deny anywhere wins over a gate
        gated = gated or verdict
    if gated:
        return gated
    if SQL_DESTRUCTIVE.search(cmd):
        return Verdict(GATE, "destructive", "destructive SQL")
    return Verdict(ALLOW)


def check_bash(cmd: str, cwd: str, ctx: Ctx) -> Verdict:
    verdict = _check_bash(cmd, cwd, ctx)
    if verdict.kind == GATE:
        # An approval must name exactly the irreversible action. A compound command would
        # make you approve a whole script, and replaying it later breaks (steps already done).
        stripped, bodies = _strip_heredocs(cmd)
        parts = len(_split(stripped)) + len(_substitutions(stripped)) + len(bodies)
        if parts > 1:
            return Verdict(DENY, "isolate",
                           f"contains an action that needs approval ({verdict.category}). Run that action alone, in its "
                           "own call, not chained with other commands (no &&, ;, |, $(...) or heredoc), so it is "
                           "recorded for approval exactly as you will run it")
    return verdict


def check_mcp(tool: str, ctx: Ctx) -> Verdict:
    for pattern in DEFAULT_MCP_ALLOW + ctx.mcp_allow:
        if re.fullmatch(pattern, tool):
            return Verdict(ALLOW)
    op = tool.rsplit("__", 1)[-1]
    if MCP_DESTRUCTIVE_OP.match(op):
        return Verdict(GATE, "destructive", f"destructive operation on an external service ({op})")
    if MCP_WRITE_OP.match(op):
        return Verdict(GATE, "third-party", f"write visible to other people ({op})")
    return Verdict(ALLOW)


def decide(tool: str, tool_input: dict, cwd: str, ctx: Ctx) -> Verdict:
    tool_input = tool_input or {}
    if tool in FORBIDDEN_TOOLS:
        return Verdict(DENY, "forbidden", FORBIDDEN_TOOLS[tool])
    if tool.startswith("mcp__"):
        return check_mcp(tool, ctx)
    if tool == "Bash":
        if tool_input.get("dangerouslyDisableSandbox"):
            return Verdict(DENY, "sandbox", "commands may not run outside the sandbox; run it normally, and if "
                                            "the sandbox blocks something the task needs, finish with status "
                                            "\"blocked\" and say what (a path, a domain)")
        return check_bash(str(tool_input.get("command", "")), cwd, ctx)
    if tool in WRITE_EDIT_TOOLS:
        raw = str(tool_input.get(WRITE_EDIT_TOOLS[tool], ""))
        target = _resolve(_expand(raw, ctx) or raw, cwd)
        if not _inside(target, ctx.write_roots):
            return Verdict(DENY, "write", f"no edits outside the worktree: {raw}")
        return Verdict(ALLOW)
    if tool in ("Read", "Grep", "Glob"):
        raw = str(tool_input.get("file_path") or tool_input.get("path") or "")
        if raw and SECRET_PATH.search(_expand(raw, ctx) or raw):
            return Verdict(DENY, "secrets", f"secret files may not be read: {raw}")
        return Verdict(ALLOW)
    if tool == "Artifact" and tool_input.get("action") == "delete":
        return Verdict(GATE, "destructive", "deleting a published artifact")
    return Verdict(ALLOW)


# --------------------------------------------------------------------------
# Repeated calls
# --------------------------------------------------------------------------

REPEAT_FILE = "repeat.json"  # reset by the runner at the start of every agent call


def _signature(tool: str, tool_input: dict) -> str:
    """What makes two calls "the same". For Bash only the command counts: the description,
    timeout and background flag are cosmetic."""
    if tool == "Bash":
        key = str(tool_input.get("command", "")).strip()
    else:
        key = json.dumps(tool_input, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(f"{tool}\0{key}".encode()).hexdigest()


def check_repeat(tool: str, tool_input: dict, ctx: Ctx) -> Verdict | None:
    """Cheap doom-loop cap: the Nth identical call in a row is denied, and so is every
    identical call after it, until the agent does something else. Best effort: if the
    counter cannot be read or written the call is not held up (this is not a safety rule)."""
    if ctx.max_identical_calls <= 0 or tool == "StructuredOutput":
        return None
    path = ctx.run_dir / REPEAT_FILE
    sig = _signature(tool, tool_input)
    try:
        state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, json.JSONDecodeError):
        state = {}
    count = int(state.get("count", 0)) + 1 if state.get("sig") == sig else 1
    try:
        path.write_text(json.dumps({"sig": sig, "count": count}), encoding="utf-8")
    except OSError:
        return None
    if count >= ctx.max_identical_calls:
        return Verdict(DENY, "repeat",
                       f"this is the same {tool} call {count} times in a row, so what you are doing is not "
                       "working. Do not repeat it: read the last result again, change your approach, or, if "
                       "you are stuck, return your final result with status \"blocked\" and say what is missing")
    return None


# --------------------------------------------------------------------------
# Hook entry point
# --------------------------------------------------------------------------

GATE_MESSAGE = (
    "⏸ Irreversible action ({category}) recorded for human approval with id {aid}. "
    "Do NOT retry it or look for another way to get the same effect. Carry on with everything that does "
    "not depend on it; when finished, return status \"needs-approval\" and explain in summary why you "
    "need it. If it is approved, this session resumes and you can run exactly the same call."
)
ASK_MESSAGE = "⏸ Harness: irreversible action ({category}: {reason}). It runs only if you approve it."


def _if_stuck(ctx: Ctx) -> str:
    """What the agent should do about a block it cannot get past."""
    if ctx.interactive:
        return "if it is essential to the task, tell your user why and ask how to proceed"
    return "if it is essential to the task, finish with status \"blocked\" and explain why"


def _audit(ctx: Ctx, tool: str, tool_input: dict, verdict: str, category: str, reason: str) -> None:
    try:
        summary = json.dumps(tool_input, ensure_ascii=False)
        if len(summary) > 600:
            summary = summary[:597] + "..."
        line = json.dumps({"ts": now(), "tool": tool, "verdict": verdict, "category": category,
                           "reason": reason, "input": summary}, ensure_ascii=False)
        with (ctx.run_dir / "audit.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def _respond(decision: str, reason: str) -> None:
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                             "permissionDecision": decision,
                                             "permissionDecisionReason": reason}}, ensure_ascii=False))


def hook_main() -> int:
    ctx_path = os.environ.get("SADYANX_GUARD_CTX")
    if not ctx_path:
        return 0  # not a sadyanx run: stay out of the way
    try:
        event = json.load(sys.stdin)
        ctx = Ctx.load(ctx_path)
    except Exception as exc:  # fail closed
        _respond("deny", f"sadyanx guard: could not read the event ({exc})")
        return 0
    tool = event.get("tool_name", "")
    tool_input = event.get("tool_input") or {}
    cwd = event.get("cwd") or os.getcwd()
    try:
        if ctx.kill_file and Path(ctx.kill_file).exists() and tool != "StructuredOutput":
            _audit(ctx, tool, tool_input, "deny", "kill", "kill switch active")
            _respond("deny", "⛔ The harness was stopped (kill switch). Do nothing else: "
                             + ("tell your user where you left off." if ctx.interactive else
                                "return your final result with status \"blocked\" and explain where you left off."))
            return 0
        repeat = check_repeat(tool, tool_input, ctx)
        if repeat:
            _audit(ctx, tool, tool_input, "deny", repeat.category, repeat.reason)
            _respond("deny", f"🔁 Nothing ran: {repeat.reason}.")
            return 0
        v = decide(tool, tool_input, cwd, ctx)
        if v.kind == ALLOW:
            _audit(ctx, tool, tool_input, "allow", v.category, v.reason)
            return 0
        if v.kind == DENY and v.category == "isolate":
            _audit(ctx, tool, tool_input, "deny", v.category, v.reason)
            _respond("deny", f"↩ Nothing ran: the command {v.reason}.")
            return 0
        if v.kind == DENY:
            _audit(ctx, tool, tool_input, "deny", v.category, v.reason)
            _respond("deny", f"⛔ Blocked by the harness ({v.category}): {v.reason}. Do not try to work around it; "
                             f"{_if_stuck(ctx)}.")
            return 0
        # GATE
        if ctx.deny_gated:
            # checked before approvals: a helper must not spend a grant meant for the task's agent
            _audit(ctx, tool, tool_input, "deny", v.category, f"helper session: {v.reason}")
            _respond("deny", f"⛔ Blocked by the harness ({v.category}): this session only reviews; it may not "
                             "run actions that need approval. Do not try another way.")
            return 0
        if approvals.consume_if_approved(ctx.run_dir, tool, tool_input):
            _audit(ctx, tool, tool_input, "allow-approved", v.category, v.reason)
            return 0
        rejected = approvals.find_rejected(ctx.run_dir, tool, tool_input)
        if rejected:
            note = rejected.get("note") or "no reason given"
            _audit(ctx, tool, tool_input, "deny-rejected", v.category, note)
            _respond("deny", f"⛔ The human rejected this action ({note}). Do not try it again.")
            return 0
        if ctx.interactive:
            # a human is watching: Claude Code shows them the call and they decide now
            # (verified: a hook's "ask" prompts even under bypassPermissions)
            _audit(ctx, tool, tool_input, "ask", v.category, v.reason)
            _respond("ask", ASK_MESSAGE.format(category=v.category, reason=v.reason))
            return 0
        item = approvals.request_tool(ctx.run_dir, tool, tool_input, v.category, v.reason)
        _audit(ctx, tool, tool_input, "gate", v.category, f"approval {item['id']}")
        _respond("deny", GATE_MESSAGE.format(category=v.category, aid=item["id"]))
        return 0
    except Exception as exc:  # fail closed
        _audit(ctx, tool, tool_input, "deny", "error", str(exc))
        _respond("deny", f"sadyanx guard failed ({exc}); the action is blocked for safety")
        return 0
