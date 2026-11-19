"""Claude Code's OS sandbox for agent runs (Seatbelt on macOS).

The guard reads commands; the sandbox confines processes. With it on, every Bash command the
agent runs, and everything that command starts (python -c, npm scripts, test runners), can
only write to the worktree, the main repo's .git (worktree commits land there), the temp dirs
and whatever the config adds; cannot read the paths in `deny_read`; and reaches the network
only through Claude Code's proxy, which lets through `allowed_domains` alone.

Not sandboxed, on purpose: setup, the gates, push/PR (the harness runs them itself) and the
guard hook (hooks run outside the sandbox, so the audit log and approvals keep working).
The file tools (Read/Edit/Write) are not covered by the sandbox either; the guard confines them.

Three settings are what make it real under bypassPermissions (see docs/DESIGN.md, D18):
  allowUnsandboxedCommands = false   otherwise the model can retry outside it, with no prompt
  failIfUnavailable = true           otherwise a sandbox that fails to start is silently skipped
  network.strictAllowlist = true     otherwise unknown domains are let through, with no prompt
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

DEFAULTS = {
    "enabled": False,
    "deny_read": ["~/.ssh", "~/.aws", "~/.config/gh", "~/.netrc", "~/.docker", "~/.gnupg", "~/.kube",
                  "~/.npmrc", "~/.pypirc", "~/.git-credentials", "~/Library/Keychains"],
    "allowed_domains": ["registry.npmjs.org", "registry.yarnpkg.com", "pypi.org", "files.pythonhosted.org",
                        "github.com", "codeload.github.com", "objects.githubusercontent.com",
                        "raw.githubusercontent.com"],
    "allow_write": [],
    "allow_local_binding": True,
    "weaker_network_isolation": False,
}

# Package-manager caches normally live in ~ (outside the writable paths). Inside the sandbox
# they are pointed at the temp dir, shared by every task.
CACHE_VARS = {
    "npm_config_cache": "npm",
    "YARN_CACHE_FOLDER": "yarn",
    "npm_config_store_dir": "pnpm",
    "PIP_CACHE_DIR": "pip",
    "UV_CACHE_DIR": "uv",
    "POETRY_CACHE_DIR": "poetry",
    "GOCACHE": "go-build",
    "GOMODCACHE": "go-mod",
    "XDG_CACHE_HOME": "xdg",
}


def options(cfg) -> dict:
    return {**DEFAULTS, **(getattr(cfg, "sandbox", None) or {})}


def enabled(cfg, repo) -> bool:
    """A repo's own `sandbox = true|false` wins; otherwise [sandbox] enabled."""
    if repo is not None and repo.sandbox is not None:
        return bool(repo.sandbox)
    return bool(options(cfg)["enabled"])


def cache_root() -> Path:
    return Path(tempfile.gettempdir()) / "sadyanx-cache"


def cache_env() -> dict:
    root = cache_root()
    return {var: str(root / sub) for var, sub in CACHE_VARS.items()}


def _abs(path: str) -> str:
    return os.path.realpath(os.path.expanduser(path))


def git_common_dir(workdir: Path) -> str | None:
    """The main repo's .git for a worktree: commits write their objects and refs there."""
    try:
        out = subprocess.run(["git", "rev-parse", "--git-common-dir"], cwd=workdir, capture_output=True,
                             text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0 or not out.stdout.strip():
        return None
    return _abs(str((Path(workdir) / out.stdout.strip())))


def settings(cfg, repo, workdir: Path) -> dict | None:
    """The `sandbox` block for a run's settings.json, or None when the sandbox is off."""
    if not enabled(cfg, repo):
        return None
    opt = options(cfg)
    write = ["/tmp", "/private/tmp", str(cache_root())]
    common = git_common_dir(workdir) if repo is not None else None
    if common and not common.startswith(_abs(str(workdir)) + os.sep):
        write.append(common)
    write += [_abs(p) for p in opt["allow_write"]]
    domains = list(opt["allowed_domains"])
    if repo is not None:
        write += [_abs(p) for p in repo.sandbox_allow_write]
        domains += list(repo.sandbox_domains)
    block = {
        "enabled": True,
        "failIfUnavailable": True,
        "allowUnsandboxedCommands": False,
        "autoAllowBashIfSandboxed": True,
        "filesystem": {
            "allowWrite": list(dict.fromkeys(write)),
            "denyRead": [_abs(p) for p in opt["deny_read"]],
        },
        "network": {
            "strictAllowlist": True,
            "allowedDomains": list(dict.fromkeys(domains)),
            "allowLocalBinding": bool(opt["allow_local_binding"]),
        },
    }
    if opt["weaker_network_isolation"]:
        block["enableWeakerNetworkIsolation"] = True
    return block


def describe(cfg, repo) -> str:
    if not enabled(cfg, repo):
        return "sandbox off"
    extra = len(repo.sandbox_domains) if repo is not None else 0
    return f"sandbox on ({len(options(cfg)['allowed_domains']) + extra} domains)"
