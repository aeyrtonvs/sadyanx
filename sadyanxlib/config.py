"""config/sadyanx.toml (global defaults) and config/repos.toml (registered repos)."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import paths
from .modes import ORDER, Mode, normalize, resolve

DEFAULTS = {
    "mode": "smart",
    "escalate": True,
    "escalation_ceiling": "smart",
    "budget_usd": 10.0,
    "timeout_min": 90,
    "max_attempts": 3,
    "gate_timeout_min": 20,
    "review_research": True,
    "claude_bin": "claude",
    "keep_awake": True,
    "run_mode": "supervised",
    "code_review": "medium",
    "code_review_mode": "smart",
}

RUN_MODES = ("supervised", "autonomous")
REVIEW_LEVELS = ("off", "low", "medium", "high", "xhigh", "max")  # /code-review's levels, or off

# The review an external agent posts as a PR comment (repos with pr_review = true).
PR_REVIEW_DEFAULTS = {"wait_min": 10, "poll_s": 60, "marker": "## Automated PR Review"}

# The PR's own CI (repos with pr_ci): how long to wait for it, how many fix rounds, and the
# Jira status a ticket moves to once its PR is ready for your review ("" = leave it).
PR_CI_DEFAULTS = {"wait_min": 30, "start_min": 10, "max_rounds": 3, "ready_jira_status": "CODE REVIEW"}
PR_CI_KINDS = ("", "pipelines", "codebuild")

SANDBOX_KEYS = ("enabled", "deny_read", "allowed_domains", "allow_write", "allow_local_binding",
                "weaker_network_isolation")

DEFAULT_PROTECTED = ["main", "master", "develop", "qa", "production", "prod", "release"]


class ConfigError(Exception):
    pass


@dataclass
class Repo:
    name: str
    path: Path
    kind: str  # personal | work
    base: str
    gates: list[str] = field(default_factory=list)
    setup: list[str] = field(default_factory=list)
    branch: str = "agent/{id}-{slug}"
    publish: str = "pr"  # pr | push | none
    pr_draft: bool = False
    bitbucket_keychain: str = ""
    sandbox: bool | None = None  # None = follow [sandbox] enabled
    sandbox_domains: list[str] = field(default_factory=list)
    sandbox_allow_write: list[str] = field(default_factory=list)
    run_mode: str = ""  # supervised | autonomous; empty = [defaults] run_mode
    code_review: str = ""  # off | low..max; empty = [defaults] code_review
    pr_review: bool = False  # wait for the external review comment on the PR and apply it
    pr_ci: str = ""  # "" | pipelines (Bitbucket) | codebuild (AWS): watch the PR's CI until it passes
    codebuild_project: str = ""  # the CodeBuild project of PR builds; {base} = the PR's target branch
    aws_profile: str = ""  # the aws CLI profile the harness (never the agent) reads CodeBuild with
    codebuild_initiator: str = ""  # who starts its PR builds (e.g. codepipeline/<pipeline>); {base} as above
    aws_region: str = ""  # empty = the profile's region
    plan_check: bool = False  # IaC: check that the CI's terraform plan is the change the task asked for


@dataclass
class Config:
    defaults: dict
    notify: dict
    guard: dict
    repos: dict[str, Repo]
    modes: dict = field(default_factory=dict)
    sandbox: dict = field(default_factory=dict)
    pr_review: dict = field(default_factory=lambda: dict(PR_REVIEW_DEFAULTS))
    pr_ci: dict = field(default_factory=lambda: dict(PR_CI_DEFAULTS))

    def mode(self, name: str | None = None, effort: str | None = None) -> "Mode":
        return resolve(name or self.defaults["mode"], self.modes, effort)

    def repo(self, name: str) -> Repo:
        if name not in self.repos:
            known = ", ".join(sorted(self.repos)) or "(none)"
            raise ConfigError(f"repo '{name}' is not registered in config/repos.toml. Registered: {known}")
        return self.repos[name]

    def code_review(self, repo: "Repo | None") -> str:
        """The /code-review level run on a code task's branch before it is published, or off."""
        return (repo.code_review if repo and repo.code_review else "") or self.defaults["code_review"]

    def run_mode(self, repo: "Repo | None") -> str:
        """supervised (a Claude session you watch) or autonomous (headless, in the background)."""
        return (repo.run_mode if repo and repo.run_mode else "") or self.defaults["run_mode"]

    @property
    def protected_branches(self) -> list[str]:
        return list(self.guard.get("protected_branches") or DEFAULT_PROTECTED)


def _read(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("rb") as fh:
        try:
            return tomllib.load(fh)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{path}: {exc}") from exc


def load() -> Config:
    # your own config/sadyanx.toml (not in git), else the example that ships with the code
    main_file = paths.config_dir() / "sadyanx.toml"
    if not main_file.exists():
        main_file = paths.config_dir() / "sadyanx.example.toml"
    main = _read(main_file)
    raw_repos = _read(paths.config_dir() / "repos.toml").get("repos", {})
    repos: dict[str, Repo] = {}
    for name, r in raw_repos.items():
        if "path" not in r or "base" not in r:
            raise ConfigError(f"repos.{name}: 'path' and 'base' are required")
        kind = r.get("kind", "personal")
        if kind not in ("personal", "work"):
            raise ConfigError(f"repos.{name}.kind must be 'personal' or 'work'")
        publish = r.get("publish", "pr")
        if publish not in ("pr", "push", "none"):
            raise ConfigError(f"repos.{name}.publish must be 'pr', 'push' or 'none'")
        repos[name] = Repo(
            name=name,
            path=Path(r["path"]).expanduser(),
            kind=kind,
            base=r["base"],
            gates=list(r.get("gates", [])),
            setup=list(r.get("setup", [])),
            branch=r.get("branch", "agent/{id}-{slug}"),
            publish=publish,
            pr_draft=bool(r.get("pr_draft", False)),
            bitbucket_keychain=r.get("bitbucket_keychain", ""),
            sandbox=r.get("sandbox"),
            sandbox_domains=list(r.get("sandbox_domains", [])),
            sandbox_allow_write=list(r.get("sandbox_allow_write", [])),
            run_mode=r.get("run_mode", ""),
            code_review=r.get("code_review", ""),
            pr_review=r.get("pr_review", False),
            pr_ci=r.get("pr_ci", ""),
            codebuild_project=r.get("codebuild_project", ""),
            aws_profile=r.get("aws_profile", ""),
            codebuild_initiator=r.get("codebuild_initiator", ""),
            aws_region=r.get("aws_region", ""),
            plan_check=r.get("plan_check", False),
        )
        repo = repos[name]
        for key in ("pr_review", "plan_check"):
            if not isinstance(getattr(repo, key), bool):
                raise ConfigError(f"repos.{name}.{key} must be true or false")
        if repo.pr_review and publish != "pr":
            raise ConfigError(f"repos.{name}.pr_review needs publish = \"pr\" (there is no PR to review otherwise)")
        if repo.pr_ci not in PR_CI_KINDS:
            raise ConfigError(f"repos.{name}.pr_ci must be \"pipelines\" or \"codebuild\" (or left out)")
        if repo.pr_ci and publish != "pr":
            raise ConfigError(f"repos.{name}.pr_ci needs publish = \"pr\" (there is no PR whose CI to watch)")
        if repo.pr_ci == "pipelines" and not repo.bitbucket_keychain:
            raise ConfigError(f"repos.{name}.pr_ci = \"pipelines\" needs bitbucket_keychain")
        if repo.pr_ci == "codebuild" and not (repo.codebuild_project and repo.aws_profile):
            raise ConfigError(f"repos.{name}.pr_ci = \"codebuild\" needs codebuild_project and aws_profile")
        if repo.plan_check and not repo.pr_ci:
            raise ConfigError(f"repos.{name}.plan_check needs pr_ci (the plan is read from the PR's CI)")
        if repos[name].run_mode and repos[name].run_mode not in RUN_MODES:
            raise ConfigError(f"repos.{name}.run_mode must be 'supervised' or 'autonomous'")
        if repos[name].code_review and repos[name].code_review not in REVIEW_LEVELS:
            raise ConfigError(f"repos.{name}.code_review must be one of {', '.join(REVIEW_LEVELS)}")
        if repos[name].sandbox is not None and not isinstance(repos[name].sandbox, bool):
            raise ConfigError(f"repos.{name}.sandbox must be true or false")
    cfg = Config(
        defaults={**DEFAULTS, **main.get("defaults", {})},
        notify=main.get("notify", {}),
        guard=main.get("guard", {}),
        repos=repos,
        modes=main.get("modes", {}),
        sandbox=main.get("sandbox", {}),
        pr_review={**PR_REVIEW_DEFAULTS, **main.get("pr_review", {})},
        pr_ci={**PR_CI_DEFAULTS, **main.get("pr_ci", {})},
    )
    unknown = set(cfg.pr_ci) - set(PR_CI_DEFAULTS)
    if unknown:
        raise ConfigError(f"sadyanx.toml [pr_ci]: unknown keys {', '.join(sorted(unknown))}")
    ci = cfg.pr_ci
    if not (isinstance(ci["wait_min"], (int, float)) and 0 < ci["wait_min"] <= 240):
        raise ConfigError("sadyanx.toml [pr_ci]: wait_min must be a number of minutes, 1 to 240")
    if not (isinstance(ci["start_min"], (int, float)) and 0 < ci["start_min"] <= ci["wait_min"]):
        raise ConfigError("sadyanx.toml [pr_ci]: start_min must be 1 minute up to wait_min")
    if not (isinstance(ci["max_rounds"], int) and not isinstance(ci["max_rounds"], bool)
            and 1 <= ci["max_rounds"] <= 10):
        raise ConfigError("sadyanx.toml [pr_ci]: max_rounds must be a whole number, 1 to 10")
    if not isinstance(ci["ready_jira_status"], str):
        raise ConfigError("sadyanx.toml [pr_ci]: ready_jira_status must be a status name, or \"\" to leave it")
    unknown = set(cfg.pr_review) - set(PR_REVIEW_DEFAULTS)
    if unknown:
        raise ConfigError(f"sadyanx.toml [pr_review]: unknown keys {', '.join(sorted(unknown))}")
    pr = cfg.pr_review
    if not (isinstance(pr["wait_min"], (int, float)) and 0 < pr["wait_min"] <= 120):
        raise ConfigError("sadyanx.toml [pr_review]: wait_min must be a number of minutes, 1 to 120")
    if not (isinstance(pr["poll_s"], (int, float)) and 10 <= pr["poll_s"] <= 600):
        raise ConfigError("sadyanx.toml [pr_review]: poll_s must be 10 to 600 seconds")
    if not (isinstance(pr["marker"], str) and pr["marker"].strip()):
        raise ConfigError("sadyanx.toml [pr_review]: marker must be the text the review comment starts with")
    if cfg.defaults["run_mode"] not in RUN_MODES:
        raise ConfigError("sadyanx.toml: [defaults] run_mode must be 'supervised' or 'autonomous'")
    if cfg.defaults["code_review"] not in REVIEW_LEVELS:
        raise ConfigError(f"sadyanx.toml: [defaults] code_review must be one of {', '.join(REVIEW_LEVELS)}")
    unknown = set(cfg.sandbox) - set(SANDBOX_KEYS)
    if unknown:
        raise ConfigError(f"sadyanx.toml [sandbox]: unknown keys {', '.join(sorted(unknown))}")
    if "escalate_last_attempt" in main.get("defaults", {}):
        raise ConfigError("sadyanx.toml: escalate_last_attempt was replaced by `escalate` (effort first, then "
                          "model, up to `escalation_ceiling`); rename it")
    try:
        normalize(cfg.defaults["mode"])
        normalize(cfg.defaults["escalation_ceiling"])
        normalize(cfg.defaults["code_review_mode"])
        for name in cfg.modes:
            if normalize(name) != name:
                raise ValueError(f"[modes.{name}]: use the full mode name, not an alias")
        for name in ORDER:  # a typo in an effort would only show when claude refuses to start
            try:
                resolve(name, cfg.modes)
            except ValueError as exc:
                raise ValueError(f"[modes.{name}]: {exc}") from exc
    except ValueError as exc:
        raise ConfigError(f"sadyanx.toml: {exc}") from exc
    return cfg
