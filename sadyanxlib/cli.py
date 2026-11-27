"""The `sadyanx` command."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

from . import approvals, config, gitops, modes, net, paths, persona, runner, sandbox, slack, status, tasks, trust, usage


def die(msg: str, code: int = 1) -> None:
    print(f"sadyanx: {msg}", file=sys.stderr)
    sys.exit(code)


def _spawn(args: list[str], task_id: str) -> None:
    """Run a sadyanx subcommand detached, so `/task run` returns right away."""
    run_dir = paths.run_dir(task_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    log = (run_dir / "runner.log").open("a")
    proc = subprocess.Popen([sys.executable, str(paths.sadyanx_bin()), *args], stdout=log, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, start_new_session=True, env=runner.agent_env(),
                            cwd=str(paths.root()))
    print(f"task {task_id} running in the background (pid {proc.pid}).")
    print(f"  progress: sadyanx log {task_id}    stop: sadyanx kill {task_id}")
    print("  you will get a notification when it finishes or needs you.")


# --- commands --------------------------------------------------------------

def cmd_add(a) -> None:
    cfg = config.load()
    if a.type == "code":
        if not a.repo:
            die("a code task needs --repo (see registered repos with: sadyanx repos)")
        cfg.repo(a.repo)
    if a.body_file == "-":
        body = sys.stdin.read()
    elif a.body_file:
        body = Path(a.body_file).read_text(encoding="utf-8")
    else:
        body = a.body or ""
    if "acceptance criteria" not in body.lower():
        print("warning: the task has no '## Acceptance criteria' section; the agent will have nothing "
              "to prove it is done against.", file=sys.stderr)
    channel = thread = ""
    if a.slack:
        try:
            channel, thread = slack.parse_origin(a.slack)
        except slack.OriginError as exc:
            die(f"--slack: {exc}")
    meta = dict(type=a.type, repo=a.repo or "", source=a.source or ("jira" if a.jira else "adhoc"),
                jira=a.jira or "", slack_channel=channel, slack_thread=thread, status=a.status, mode=modes.normalize(a.mode or cfg.defaults["mode"]),
                effort=a.effort or "",
                budget_usd=a.budget if a.budget is not None else cfg.defaults["budget_usd"],
                timeout_min=a.timeout if a.timeout is not None else cfg.defaults["timeout_min"])
    task = tasks.create(a.title, body, **meta)
    print(f"{task.id}\t{task.path}")
    if channel:
        print(f"reply on Slack: {slack.where(channel, thread)}, after your approval")


def _find(ref: str) -> tasks.Task:
    """tasks.find, but a task left `running` by a dead runner is first marked `killed`."""
    t = tasks.find(ref)
    if runner.recover_stale(t):
        print(f"task {t.id} was interrupted (its runner is gone); marked killed. "
              f"`sadyanx run {t.id}` resumes it.", file=sys.stderr)
    return t


def cmd_list(a) -> None:
    rows = tasks.all_tasks()
    for t in rows:
        runner.recover_stale(t)
    if a.active:
        rows = [t for t in rows if t.status not in ("done",)]
    if not rows:
        print("no tasks")
        return
    print(f"{'ID':<5} {'STATUS':<15} {'TYPE':<9} {'MODE':<14} {'REPO':<18} {'COST':>7}  TITLE")
    for t in rows:
        repo = t.get("repo") or "-"
        mode = (t.get("mode") or "-") + (f"/{t.get('effort')}" if t.get("effort") else "")
        print(f"{t.id:<5} {t.status:<15} {t.type:<9} {mode:<14} {repo[:18]:<18} "
              f"{'$%.2f' % t.num('cost_usd'):>7}  {t.title[:70]}")
    pending = [(t, approvals.pending(paths.run_dir(t.id))) for t in rows if t.status == "needs-approval"]
    for t, items in pending:
        for i in items:
            print(f"  ⏸ {t.id} {approvals.describe(i)}")


def cmd_show(a) -> None:
    t = _find(a.ref)
    print(t.path.read_text(encoding="utf-8"))
    run_dir = paths.run_dir(t.id)
    items = approvals.load(run_dir)
    if items:
        print("## Approvals")
        for i in items:
            print(f"- {i['status']}: {approvals.describe(i)}")
    rep = run_dir / "report.md"
    if rep.exists():
        print(f"\nReport: {rep}")


SUPERVISED_ELSEWHERE = 3  # exit code: the run needs a terminal; /task opens one


def cmd_run(a) -> None:
    t = _find(a.ref)
    try:
        # --fg is the headless run in the foreground, so it means autonomous unless --watch
        run_mode = runner.resolve_run_mode(t, config.load(), a.watch, a.auto or a.fg)
    except config.ConfigError as exc:
        die(str(exc))
    if run_mode == "supervised" and not runner.has_terminal():
        cmd = f"{paths.sadyanx_bin()} run {t.id}" + (" --watch" if a.watch else "") + \
              (f" --mode {modes.normalize(a.mode)}" if a.mode else "") + (f" --effort {a.effort}" if a.effort else "")
        print(f"task {t.id} runs supervised: it opens a Claude session you watch and answer in a terminal.")
        print(f"  run it in a terminal: {cmd}")
        print(f"  or headless in the background: sadyanx run {t.id} --auto")
        sys.exit(SUPERVISED_ELSEWHERE)
    if a.fg or run_mode == "supervised":
        (paths.pids_dir() / f"{t.id}.kill").unlink(missing_ok=True)
        try:
            status = runner.run_task(a.ref, a.mode, run_mode, a.effort)
        except (runner.RunError, config.ConfigError) as exc:
            die(str(exc))
        print(f"task {t.id}: {status}")
        return
    # validate before detaching so errors show up here, not in a log
    if paths.kill_file().exists():
        die("the kill switch is on (sadyanx unkill to clear it)")
    if t.status in ("draft", "done"):
        die(f"the task is {t.status}")
    if t.status in ("running", "verifying"):
        die("the task is already running")
    if approvals.pending(paths.run_dir(t.id)):
        die(f"approvals pending: sadyanx approve {t.id} / sadyanx reject {t.id}")
    if t.type == "code":
        config.load().repo(t.get("repo"))
    extra = (["--mode", modes.normalize(a.mode)] if a.mode else []) + (["--effort", a.effort] if a.effort else [])
    _spawn(["run", t.id, "--fg", *extra], t.id)


def _decide(a, status: str) -> None:
    # a decision carries out harness actions (push, open a PR, send on Slack) and resumes the
    # agent, none of which may happen while everything is meant to be stopped
    if paths.kill_file().exists():
        die("the kill switch is on: nothing is approved or rejected until you clear it (sadyanx unkill)")
    t = tasks.find(a.ref)
    run_dir = paths.run_dir(t.id)
    pend = approvals.pending(run_dir)
    if not pend:
        die(f"task {t.id} has no pending approvals")
    if a.all:
        ids = None
    elif a.ids:
        ids = a.ids
    elif len(pend) == 1:
        ids = [pend[0]["id"]]
    else:
        for i in pend:
            print(approvals.describe(i))
        die("several approvals are pending: give their ids or use --all")
    changed = approvals.decide(run_dir, ids, status, getattr(a, "reason", "") or "")
    for i in changed:
        print(f"{status}: {approvals.describe(i)}")
    left = approvals.pending(run_dir)
    if left:
        print(f"{len(left)} still pending; the task waits until all of them are decided")
        return
    cfg = config.load()
    final = runner.apply_harness_approvals(t, cfg)
    if final:
        print(f"task {t.id}: {final}")
        return
    if t.get("phase") == "pr-review":
        print(f"PR opened: {t.get('pr_url')}. {t.get('reason') or 'Watching it'}; the agent applies what comes up.")
    if a.fg:  # approvals belong to headless runs, so the resumed run is headless too
        print(f"task {t.id}: {runner.run_task(t.id, run_mode='autonomous')}")
    else:
        _spawn(["run", t.id, "--fg"], t.id)


def cmd_approve(a) -> None:
    _decide(a, "approved")


def cmd_reject(a) -> None:
    _decide(a, "rejected")


def _kill_pid_file(pid_file: Path) -> bool:
    try:
        pid = int(pid_file.read_text().strip())
        os.killpg(pid, signal.SIGTERM)
        return True
    except (OSError, ValueError):
        return False


def cmd_kill(a) -> None:
    paths.pids_dir().mkdir(parents=True, exist_ok=True)
    if a.ref:
        t = tasks.find(a.ref)
        (paths.pids_dir() / f"{t.id}.kill").write_text(tasks.now())
        hit = _kill_pid_file(paths.pids_dir() / f"{t.id}.agent")
        print(f"task {t.id}: {'agent stopped' if hit else 'no agent was running'}")
        return
    paths.kill_file().write_text(tasks.now())
    hits = [p.stem for p in paths.pids_dir().glob("*.agent") if _kill_pid_file(p)]
    print("kill switch ON: the guard denies every action and no new task starts.")
    print(f"agents stopped: {', '.join(hits) or 'none running'}")
    print("to resume: sadyanx unkill")


def cmd_unkill(a) -> None:
    paths.kill_file().unlink(missing_ok=True)
    print("kill switch off")


def cmd_log(a) -> None:
    t = tasks.find(a.ref)
    f = paths.run_dir(t.id) / "events.log"
    if not f.exists():
        die("the task has no events yet")
    lines = f.read_text(encoding="utf-8").splitlines()
    print("\n".join(lines[-a.n:]))


def cmd_clean(a) -> None:
    t = _find(a.ref)
    if t.status in ("running", "verifying"):
        die("the task is running")
    if t.status not in ("done", "failed", "killed") and not a.force:
        die(f"the task is {t.status}; use --force to delete its worktree anyway")
    wt = Path(t.get("worktree") or "")
    if not t.get("worktree") or not wt.exists():
        print("no worktree to delete")
        return
    if t.type == "code":
        gitops.remove_worktree(config.load().repo(t.get("repo")).path, wt, force=a.force)
    else:
        shutil.rmtree(wt)
    print(f"worktree deleted: {wt} (branch {t.get('branch') or '-'} is kept)")


def cmd_usage(a) -> None:
    if a.review:
        print(usage.review(config.load(), a.days))
    elif a.json:
        print(usage.as_json(a.days))
    else:
        print(usage.render(a.days))


def cmd_status(a) -> None:
    print(status.as_json(a.days) if a.json else status.render(a.days))


def cmd_mcp(a) -> None:
    from . import mcp_server
    if a.config:  # what to add to Claude Code's MCP configuration
        print(json.dumps({"type": "stdio", "command": sys.executable, "args": [str(paths.sadyanx_bin()), "mcp"]}))
        return
    mcp_server.serve(dict(os.environ))


def cmd_persona(a) -> None:
    print(persona.describe())


def cmd_repos(a) -> None:
    cfg = config.load()
    if not cfg.repos:
        print("no repos registered in config/repos.toml")
    for r in cfg.repos.values():
        gates = " && ".join(r.gates) or "(no gates)"
        print(f"{r.name:<20} {r.kind:<9} base={r.base:<8} publish={r.publish:<5} {r.path}\n{'':<20} gates: {gates}"
              f"\n{'':<20} {sandbox.describe(cfg, r)}")


def _aws_profile_ok(profile: str, region: str) -> tuple[bool, str]:
    """Whether the aws CLI can use this profile now (an SSO session expires)."""
    if not shutil.which("aws"):
        return False, "the aws CLI is not installed"
    args = ["aws", "sts", "get-caller-identity", "--profile", profile] + (["--region", region] if region else [])
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    if proc.returncode == 0:
        return True, ""
    err = (proc.stderr or proc.stdout).strip().splitlines()
    return False, (err[-1] if err else "failed") + f" (aws sso login --profile {profile})"


def cmd_doctor(a) -> None:
    ok = True

    def check(cond: bool, good: str, bad: str) -> None:
        nonlocal ok
        print(("  ✓ " + good) if cond else ("  ✗ " + bad))
        ok = ok and cond

    print("sadyanx doctor")
    claude = shutil.which("claude") or str(Path.home() / ".local/bin/claude")
    check(Path(claude).exists(), f"claude: {claude}", "claude binary not found")
    gh = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True) if shutil.which("gh") else None
    check(bool(gh and gh.returncode == 0), "gh authenticated (GitHub PRs)", "gh is not authenticated")
    try:
        cfg = config.load()
        check(True, f"config valid ({len(cfg.repos)} repos)", "")
    except config.ConfigError as exc:
        check(False, "", f"invalid config: {exc}")
        return
    for r in cfg.repos.values():
        exists = r.path.exists() and gitops.ok("rev-parse", "--git-dir", cwd=r.path)
        check(exists, f"repo {r.name}: {r.path}", f"repo {r.name}: {r.path} does not exist or is not git")
        if not exists:
            continue
        base_ok = gitops.ok("rev-parse", "--verify", f"origin/{r.base}", cwd=r.path) or \
            gitops.ok("rev-parse", "--verify", r.base, cwd=r.path)
        check(base_ok, f"  base {r.base} exists", f"  base {r.base} does not exist in {r.name}")
        print(f"  · {sandbox.describe(cfg, r)}")
        if cfg.run_mode(r) == "supervised":
            check(trust.trusted(r.path, git_repo=r.path), "  trusted in Claude Code (supervised runs start without asking)",
                  "  not trusted in Claude Code: supervised runs will ask every time (sadyanx install)")
        host = gitops.host_of(gitops.remote_url(r.path))[0]
        if r.publish == "pr" and host == "bitbucket":
            if r.bitbucket_keychain:
                found = subprocess.run(["security", "find-generic-password", "-s", r.bitbucket_keychain],
                                       capture_output=True).returncode == 0
                check(found, f"  Bitbucket credentials in the keychain ({r.bitbucket_keychain})",
                      f"  '{r.bitbucket_keychain}' missing from the keychain")
            else:
                check(False, "", "  Bitbucket repo without bitbucket_keychain: cannot open PRs")
        if r.pr_review:
            check(host == "bitbucket" and bool(r.bitbucket_keychain),
                  f"  applies the external PR review (waits up to {cfg.pr_review['wait_min']} min for it)",
                  "  pr_review is on, but only Bitbucket PRs with bitbucket_keychain are watched")
        if r.pr_ci:
            check(host == "bitbucket",
                  f"  watches the PR's CI ({r.pr_ci}; up to {cfg.pr_ci['max_rounds']} fix rounds"
                  + (", checks the plan" if r.plan_check else "") + ")",
                  "  pr_ci is on, but only Bitbucket PRs are watched")
            if r.pr_ci == "codebuild":
                good, why = _aws_profile_ok(r.aws_profile, r.aws_region)
                check(good, f"  AWS profile {r.aws_profile} can read CodeBuild",
                      f"  AWS profile {r.aws_profile}: {why}")
    cas = net.ca_source()
    check(cas is not None,
          "TLS certificates for HTTPS (Bitbucket PRs, ntfy): "
          + (cas if cas == net.PYTHON_DEFAULT else f"Python has none, using {cas}"),
          "no TLS certificates: Bitbucket PRs and ntfy will fail (run Python's 'Install Certificates.command')")
    check(not paths.kill_file().exists(), "kill switch off", "kill switch ON (sadyanx unkill)")
    cmd_link = Path.home() / ".claude/commands/task.md"
    check(cmd_link.exists(), "/task slash command installed", "/task missing: sadyanx install")
    check(trust.trusted(paths.workspaces_dir()), f"Claude Code trusts {paths.state_dir()} (supervised research runs)",
          f"Claude Code does not trust {paths.state_dir()}: supervised research runs will ask every time "
          "(sadyanx install)")
    if not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        print("  · for long unattended runs use a long-lived token: claude setup-token")
    sys.exit(0 if ok else 1)


def cmd_install(a) -> None:
    # your config lives next to the code but out of git: start it from the examples
    for name in ("sadyanx.toml", "repos.toml", "persona.toml"):
        mine, example = paths.config_dir() / name, paths.config_dir() / name.replace(".toml", ".example.toml")
        if not mine.exists() and example.exists():
            shutil.copy(example, mine)
            print(f"created {mine} from {example.name}: edit it (it is not tracked by git)")
    links = {
        Path.home() / ".claude/commands/task.md": paths.CODE / "commands/task.md",
        Path.home() / ".local/bin/sadyanx": paths.sadyanx_bin(),
    }
    for dst, src in links.items():
        if dst.is_symlink() and dst.resolve() == src.resolve():
            print(f"already installed: {dst}")
            continue
        if dst.exists() or dst.is_symlink():
            if not a.force:
                print(f"exists and is not ours, leaving it: {dst} (use --force to replace it)")
                continue
            dst.unlink()
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.symlink_to(src)
        print(f"installed: {dst} -> {src}")
    # Supervised runs open an interactive claude in a fresh folder each time. Worktrees are
    # trusted through their main repo; research workspaces through the state dir (a parent).
    state = paths.state_dir()
    state.mkdir(parents=True, exist_ok=True)
    try:
        repos = [r.path for r in config.load().repos.values() if r.path.exists()]
    except config.ConfigError:
        repos = []
    for folder in [state, *repos]:
        try:
            changed = trust.trust(folder)
        except (OSError, ValueError) as exc:
            print(f"could not mark {folder} trusted in Claude Code ({exc}); supervised runs there will ask: "
                  "choose 'Yes, I trust this folder'")
            return
        print(f"{'trusted' if changed else 'already trusted'} in Claude Code: {folder}")


# --- parser ------------------------------------------------------------------

MODE_HELP = "ultra | smart | balanced | cheap (deep = ultra, fast = balanced)"


EFFORT_HELP = " | ".join(modes.EFFORTS) + " (default: the mode's own effort)"


def _mode_arg(value: str) -> str:
    try:
        return modes.normalize(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _effort_arg(value: str) -> str:
    try:
        return modes.normalize_effort(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sadyanx", description="sadyanx: a personal harness for autonomous Claude Code agents")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("add", help="add a task to the queue")
    s.add_argument("--title", required=True)
    s.add_argument("--type", choices=["code", "research"], default="code")
    s.add_argument("--repo")
    s.add_argument("--jira")
    s.add_argument("--slack", metavar="LINK", help="the Slack message that asked for it (its link, or "
                   "CHANNEL/TS): the agent drafts a reply and the harness sends it there after your approval")
    s.add_argument("--source", choices=["jira", "adhoc"])
    s.add_argument("--status", choices=["ready", "draft"], default="ready")
    s.add_argument("--budget", type=float, help="USD")
    s.add_argument("--timeout", type=int, help="minutes")
    s.add_argument("--mode", type=_mode_arg, help=MODE_HELP)
    s.add_argument("--effort", type=_effort_arg, help="stored in the task: " + EFFORT_HELP)
    s.add_argument("--body-file", help="path, or '-' for stdin")
    s.add_argument("--body")
    s.set_defaults(fn=cmd_add)

    s = sub.add_parser("list", help="show the queue")
    s.add_argument("--active", action="store_true", help="hide finished tasks")
    s.set_defaults(fn=cmd_list)

    s = sub.add_parser("show", help="show one task")
    s.add_argument("ref")
    s.set_defaults(fn=cmd_show)

    s = sub.add_parser("run", help="run a task: supervised in this terminal, or autonomous in the background "
                                   "(per run_mode in the config)")
    s.add_argument("ref")
    which = s.add_mutually_exclusive_group()
    which.add_argument("--watch", action="store_true", help="this run: supervised (a Claude session you watch)")
    which.add_argument("--auto", action="store_true", help="this run: autonomous (headless, in the background)")
    s.add_argument("--fg", action="store_true", help="autonomous, but in the foreground")
    s.add_argument("--mode", type=_mode_arg, help="this run only: " + MODE_HELP)
    s.add_argument("--effort", type=_effort_arg, help="this run only (no automatic step-up for earlier "
                                                      "failures): " + EFFORT_HELP)
    s.set_defaults(fn=cmd_run)

    for name, fn, help_ in (("approve", cmd_approve, "approve pending actions and resume"),
                            ("reject", cmd_reject, "reject pending actions and resume")):
        s = sub.add_parser(name, help=help_)
        s.add_argument("ref")
        s.add_argument("ids", nargs="*")
        s.add_argument("--all", action="store_true")
        s.add_argument("--fg", action="store_true")
        if name == "reject":
            s.add_argument("--reason", default="")
        s.set_defaults(fn=fn)

    s = sub.add_parser("kill", help="stop one task, or everything (kill switch) with no argument")
    s.add_argument("ref", nargs="?")
    s.set_defaults(fn=cmd_kill)

    sub.add_parser("unkill", help="clear the kill switch").set_defaults(fn=cmd_unkill)

    s = sub.add_parser("log", help="a task's events")
    s.add_argument("ref")
    s.add_argument("-n", type=int, default=40)
    s.set_defaults(fn=cmd_log)

    s = sub.add_parser("clean", help="delete a finished task's worktree")
    s.add_argument("ref")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_clean)

    s = sub.add_parser("usage", help="where the tokens go (and, with --review, what to change)")
    s.add_argument("--days", type=int, default=30)
    s.add_argument("--json", action="store_true")
    s.add_argument("--review", action="store_true", help="an agent (balanced mode) proposes savings")
    s.set_defaults(fn=cmd_usage)

    s = sub.add_parser("status", help="queue, approvals, kill switch, guard and usage in one snapshot (the dashboard reads --json)")
    s.add_argument("--days", type=int, default=30)
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("mcp", help="local MCP server (stdio) for the desktop dashboard: status, approve, reject")
    s.add_argument("--config", action="store_true", help="print the server entry for Claude Code's MCP config")
    s.set_defaults(fn=cmd_mcp)

    sub.add_parser("persona", help="the voice sadyanx uses with you (config/persona.toml)").set_defaults(fn=cmd_persona)

    sub.add_parser("repos", help="registered repos").set_defaults(fn=cmd_repos)
    sub.add_parser("doctor", help="check installation and config").set_defaults(fn=cmd_doctor)

    s = sub.add_parser("install", help="install /task and the harness command")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_install)
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        args.fn(args)
    except (LookupError, config.ConfigError, runner.RunError, gitops.GitError) as exc:
        die(str(exc))
