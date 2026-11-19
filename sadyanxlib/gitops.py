"""Git and code-host operations the harness performs itself (never the agent):
worktree setup, pushing the task branch, opening the PR."""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

from . import net


class GitError(Exception):
    pass


def git(*args: str, cwd: Path | str, check: bool = True, timeout: int = 120) -> str:
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=timeout)
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout.strip()


def ok(*args: str, cwd: Path | str) -> bool:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True).returncode == 0


def ensure_worktree(repo: Path, worktree: Path, branch: str, base: str, log) -> str:
    """Create (or reuse) the task's worktree on its own branch. Returns the start point."""
    if worktree.exists() and (worktree / ".git").exists():
        log(f"reusing worktree {worktree}")
        return ""
    if not ok("rev-parse", "--git-dir", cwd=repo):
        raise GitError(f"{repo} is not a git repo")
    has_origin = ok("remote", "get-url", "origin", cwd=repo)
    if has_origin:
        try:
            git("fetch", "origin", base, cwd=repo, timeout=300)
        except (GitError, subprocess.TimeoutExpired) as exc:
            log(f"warning: could not fetch origin/{base} ({exc}); using the local branch")
    start = f"origin/{base}" if has_origin and ok("rev-parse", "--verify", f"origin/{base}", cwd=repo) else base
    if not ok("rev-parse", "--verify", start, cwd=repo):
        raise GitError(f"base branch '{base}' does not exist in {repo}")
    worktree.parent.mkdir(parents=True, exist_ok=True)
    if ok("rev-parse", "--verify", f"refs/heads/{branch}", cwd=repo):
        git("worktree", "add", str(worktree), branch, cwd=repo)
    else:
        git("worktree", "add", "--no-track", "-b", branch, str(worktree), start, cwd=repo)
    log(f"worktree {worktree} on branch {branch} from {start}")
    return start


def remove_worktree(repo: Path, worktree: Path, force: bool) -> None:
    args = ["worktree", "remove", str(worktree)] + (["--force"] if force else [])
    git(*args, cwd=repo)


def base_ref(worktree: Path, base: str) -> str:
    """The ref the task branch is compared against: origin/<base> when it exists."""
    return f"origin/{base}" if ok("rev-parse", "--verify", f"origin/{base}", cwd=worktree) else base


def commits_ahead(worktree: Path, base: str) -> list[str]:
    ref = base_ref(worktree, base)
    out = git("log", "--format=%h %s", f"{ref}..HEAD", cwd=worktree, check=False)
    return [line for line in out.splitlines() if line.strip()]


def dirty(worktree: Path) -> list[str]:
    out = git("status", "--porcelain", cwd=worktree, check=False)
    return [line for line in out.splitlines() if line.strip()]


def push(worktree: Path, branch: str) -> None:
    git("push", "-u", "origin", f"HEAD:refs/heads/{branch}", cwd=worktree, timeout=300)


def remote_url(worktree: Path) -> str:
    return git("remote", "get-url", "origin", cwd=worktree, check=False)


def host_of(url: str) -> tuple[str, str, str]:
    """('github'|'bitbucket'|'other', owner/workspace, repo slug)."""
    m = re.search(r"(github\.com|bitbucket\.org)[:/]([^/]+)/([^/]+?)(?:\.git)?/?$", url)
    if not m:
        return "other", "", ""
    host = "github" if m.group(1) == "github.com" else "bitbucket"
    return host, m.group(2), m.group(3)


def _clean_env() -> dict:
    return {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE", "SADYANX_GUARD"))}


def open_github_pr(worktree: Path, base: str, branch: str, title: str, body: str, draft: bool) -> str:
    args = ["gh", "pr", "create", "--base", base, "--head", branch, "--title", title, "--body", body]
    if draft:
        args.append("--draft")
    proc = subprocess.run(args, cwd=str(worktree), capture_output=True, text=True, timeout=120, env=_clean_env())
    if proc.returncode != 0:
        raise GitError(f"gh pr create: {proc.stderr.strip()}")
    return proc.stdout.strip().splitlines()[-1]


def _keychain(service: str) -> tuple[str, str]:
    """Bitbucket credentials from the macOS keychain, never from a file."""
    try:
        meta = subprocess.run(["security", "find-generic-password", "-s", service],
                              capture_output=True, text=True, timeout=10).stdout
        user = re.search(r'"acct"<blob>="(.*)"', meta)
        token = subprocess.run(["security", "find-generic-password", "-s", service, "-w"],
                               capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        raise GitError(f"could not read keychain item '{service}': {exc}") from exc
    if not user or not token:
        raise GitError(f"no credentials in keychain item '{service}' "
                       f"(security add-generic-password -s {service} -a <email> -w)")
    return user.group(1), token


def _bitbucket_auth(keychain_service: str) -> str:
    user, token = _keychain(keychain_service)
    return "Basic " + base64.b64encode(f"{user}:{token}".encode()).decode()


def bitbucket_pr_id(url: str) -> int | None:
    m = re.search(r"bitbucket\.org/[^/]+/[^/]+/pull-requests/(\d+)", url or "")
    return int(m.group(1)) if m else None


def bitbucket_pr_comments(workspace: str, slug: str, pr_id: int, keychain_service: str) -> list[dict]:
    """Every comment on a PR, oldest first (read-only)."""
    auth = _bitbucket_auth(keychain_service)
    url = (f"https://api.bitbucket.org/2.0/repositories/{workspace}/{slug}/pullrequests/{pr_id}"
           "/comments?pagelen=100&sort=created_on")
    comments: list[dict] = []
    for _ in range(20):  # 2000 comments is far more than any PR has
        req = urllib.request.Request(url)
        req.add_header("Authorization", auth)
        try:
            with net.urlopen(req, timeout=30) as resp:
                page = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            raise GitError(f"Bitbucket answered {exc.code}: {exc.read().decode(errors='replace')[:300]}") from exc
        comments += page.get("values") or []
        url = page.get("next")
        if not url:
            break
    return comments


def open_bitbucket_pr(workspace: str, slug: str, base: str, branch: str, title: str, body: str,
                      keychain_service: str) -> str:
    auth = _bitbucket_auth(keychain_service)
    payload = {
        "title": title,
        "description": body,
        "source": {"branch": {"name": branch}},
        "destination": {"branch": {"name": base}},
        "close_source_branch": True,
    }
    url = f"https://api.bitbucket.org/2.0/repositories/{workspace}/{slug}/pullrequests"
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", auth)
    try:
        with net.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:500]
        raise GitError(f"Bitbucket answered {exc.code}: {detail}") from exc
    return data.get("links", {}).get("html", {}).get("href", "")
