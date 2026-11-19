#!/usr/bin/env python3
"""Checks that Claude Code's sandbox, as the harness configures it, holds on this machine.

One real Haiku agent (about a cent) runs a fixed list of commands inside the sandbox, with no
guard, so that only the sandbox is under test. The verdicts come from the filesystem, not from
what the agent says. Everything happens in a throwaway repo under ~/.sadyanx (outside $TMPDIR,
which the sandbox leaves writable) and is deleted afterwards.

    python3 tests/sandbox_probe.py          run it
    KEEP=1 python3 tests/sandbox_probe.py   keep the probe directory for inspection

Exit code 0 only if every check passes. Turn the sandbox on for a repo only after that.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sadyanxlib import config, paths, sandbox  # noqa: E402
from sadyanxlib.runner import agent_env  # noqa: E402


def git(*args, cwd):
    subprocess.run(["git", "-c", "user.email=probe@sadyanx", "-c", "user.name=probe", *args], cwd=cwd,
                   check=True, capture_output=True)


def main() -> int:
    cfg = config.load()
    tag = secrets.token_hex(4)
    base = paths.state_dir() / f"sandbox-probe-{tag}"
    repo_dir, wt, outside = base / "repo", base / "wt", base / "outside"
    for d in (repo_dir, outside):
        d.mkdir(parents=True)
    git("init", "-q", "-b", "main", cwd=repo_dir)
    (repo_dir / "README.md").write_text("probe\n")
    git("add", "-A", cwd=repo_dir)
    git("commit", "-qm", "init", cwd=repo_dir)
    git("worktree", "add", "-q", "-b", "probe", str(wt), cwd=repo_dir)

    repo = config.Repo(name="probe", path=repo_dir, kind="personal", base="main", sandbox=True)
    box = sandbox.settings(cfg, repo, wt)
    settings = base / "settings.json"
    settings.write_text(json.dumps({"sandbox": box}, indent=2))
    tmp_file = Path("/tmp") / f"sadyanx-probe-{tag}"
    ssh = Path.home() / ".ssh"
    allowed = box["network"]["allowedDomains"]
    allowed_url = "https://registry.npmjs.org/left-pad" if "registry.npmjs.org" in allowed else None

    commands = [
        f"python3 -c \"open('{outside}/escape.txt','w').write('x')\" 2> escape.err",
        f"(echo x > {outside}/redirect.txt) 2> redirect.err",
        "python3 -c \"import os; print(os.listdir(os.path.expanduser('~/.ssh')))\" > ssh.txt 2>&1",
        "curl -sS -m 20 -o blocked.html https://example.com 2> blocked.err",
        "echo hi > a.txt && git add a.txt && git -c user.email=p@h -c user.name=p commit -qm probe",
        f"python3 -c \"open('{tmp_file}','w').write('x')\"",
        "echo \"$npm_config_cache\" > cache.txt",
    ]
    if allowed_url:
        commands.append(f"curl -sS -m 20 -o allowed.json {allowed_url}")
    prompt = ("You are testing a sandbox. Run each of these shell commands with the Bash tool, one call per "
              "command, exactly as written, in this order. Some are expected to fail: do not fix, retry or "
              "work around anything, and never set dangerouslyDisableSandbox. When all have run, reply "
              "\"done\".\n\n" + "\n".join(f"{i}. `{c}`" for i, c in enumerate(commands, 1)))
    claude = shutil.which(cfg.defaults["claude_bin"]) or str(Path.home() / ".local/bin/claude")
    args = [claude, "-p", prompt, "--model", "haiku", "--effort", "low", "--tools", "Bash",
            "--strict-mcp-config", "--permission-mode", "bypassPermissions", "--settings", str(settings),
            "--no-session-persistence", "--output-format", "json", "--max-budget-usd", "0.5"]
    print(f"probe dir: {base}\nrunning a Haiku agent inside the sandbox...")
    proc = subprocess.run(args, cwd=wt, capture_output=True, text=True, timeout=600, stdin=subprocess.DEVNULL,
                          env=agent_env(sandbox.cache_env()))
    try:
        result = json.loads(proc.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        result = None

    def text(name: str) -> str:
        p = wt / name
        return p.read_text(errors="replace") if p.exists() else ""

    commits = subprocess.run(["git", "rev-list", "--count", "HEAD"], cwd=wt, capture_output=True,
                             text=True).stdout.strip()
    def refused(name: str) -> bool:
        """The command ran (it left its error output) and was refused."""
        return bool(text(name).strip())

    checks = [
        ("the agent ran (sandbox started)", result is not None and not result.get("is_error"),
         f"exit {proc.returncode}: {(proc.stderr or proc.stdout).strip()[:300]}"),
        ("no write outside the worktree (python)", not (outside / "escape.txt").exists() and refused("escape.err"),
         "escape.txt was written" if (outside / "escape.txt").exists() else "the command did not run"),
        ("no write outside the worktree (redirect)",
         not (outside / "redirect.txt").exists() and refused("redirect.err"),
         "redirect.txt was written" if (outside / "redirect.txt").exists() else "the command did not run"),
        ("~/.ssh is not readable", "Operation not permitted" in text("ssh.txt") if ssh.is_dir() else True,
         f"expected a PermissionError, got: {text('ssh.txt').strip()[:120] or '(the command did not run)'}"),
        ("a domain off the list is blocked", not text("blocked.html").strip() and refused("blocked.err"),
         "example.com was reachable (strictAllowlist not in effect?)" if text("blocked.html").strip()
         else "the command did not run"),
        ("git commit works in the worktree", commits == "2", f"HEAD has {commits or '?'} commit(s), expected 2"),
        ("/tmp is writable", tmp_file.exists(), f"{tmp_file} was not written"),
        ("package caches point at the temp dir", str(sandbox.cache_root()) in text("cache.txt"),
         f"npm_config_cache inside the sandbox: {text('cache.txt').strip() or '(empty)'}"),
    ]
    if allowed_url:
        checks.append(("a domain on the list is reachable", '"name"' in text("allowed.json"),
                       "registry.npmjs.org was not reachable"))

    print()
    for name, passed, why in checks:
        print(f"  {'✓' if passed else '✗'} {name}" + ("" if passed else f" — {why}"))
    if result is not None:
        print(f"\ncost ${float(result.get('total_cost_usd') or 0):.3f}, {result.get('num_turns')} turns")

    tmp_file.unlink(missing_ok=True)
    if os.environ.get("KEEP"):
        print(f"kept {base}")
    else:
        subprocess.run(["git", "worktree", "remove", "--force", str(wt)], cwd=repo_dir, capture_output=True)
        shutil.rmtree(base, ignore_errors=True)
    failed = [c for c in checks if not c[1]]
    print("\nSANDBOX PROBE " + ("OK" if not failed else f"FAILED ({len(failed)} of {len(checks)})"))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
