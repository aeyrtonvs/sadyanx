"""A PR's CI, read by the harness itself (never the agent): Bitbucket Pipelines through the
Bitbucket API, or AWS CodeBuild through the aws CLI with a profile of yours. Read-only, plus
one retry of a run that failed for reasons outside the change.

Both readers answer the same three questions for the PR's head commit: the newest CI run
(`latest`; `also` is a rerun's ref, found even if it does not look like a PR run), the logs of
its failed steps or of its plan (`logs`), and run it again (`retry`, which returns the rerun's
ref)."""

from __future__ import annotations

import json
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from . import gitops, net

LOG_MAX_CHARS = 60_000      # per step, for the prompt; the full log goes to the run dir
PLAN_MAX_CHARS = 150_000    # all plan text given to the plan check
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
PLAN_START = re.compile(r"Terraform will perform the following actions|Objects have changed outside of Terraform"
                        r"|No changes\. Your infrastructure matches|Terraform used the selected providers")
PLAN_END = re.compile(r"^\s*(Plan: \d+ to add|No changes\. Your infrastructure matches|Changes to Outputs)")


class CiError(Exception):
    """CI could not be read. `login` is set when the fix is yours: an expired AWS SSO session."""

    def __init__(self, message: str, login: str = ""):
        super().__init__(message)
        self.login = login


@dataclass
class Step:
    name: str
    state: str          # passed | failed | stopped | skipped | running | pending
    ref: str = ""       # the step's uuid (Bitbucket) or "" (CodeBuild has one log)
    log: str = ""


@dataclass
class CiRun:
    state: str          # running | passed | failed | stopped
    ref: str            # pipeline uuid / CodeBuild build id
    url: str
    commit: str
    created: str = ""
    steps: list[Step] = field(default_factory=list)
    raw: dict = field(default_factory=dict)

    @property
    def done(self) -> bool:
        return self.state != "running"

    @property
    def failed_steps(self) -> list[Step]:
        return [s for s in self.steps if s.state == "failed"]


def clean(text: str) -> str:
    return ANSI.sub("", text).replace("\r\n", "\n")


def tail(text: str, limit: int = LOG_MAX_CHARS) -> str:
    """The end of a log, where a failure usually shows, with how much was cut."""
    text = clean(text).strip()
    if len(text) <= limit:
        return text
    cut = text[-limit:]
    return f"[... {len(text) - limit} earlier characters cut ...]\n" + cut[cut.find("\n") + 1:]


def plan_excerpt(text: str, limit: int = PLAN_MAX_CHARS) -> str:
    """The plan in a CI log: each block from 'Terraform will perform...' (or the drift note)
    to its 'Plan: N to add...' line. The whole log when it fits; '' when there is no plan."""
    text = clean(text)
    if not PLAN_START.search(text):
        return ""
    if len(text) <= limit:
        return text.strip()
    lines, out, inside = text.splitlines(), [], False
    for line in lines:
        if PLAN_START.search(line):
            inside = True
        if inside:
            out.append(line)
            if PLAN_END.match(line):
                inside = False
                out.append("")
    excerpt = "\n".join(out).strip()
    if len(excerpt) > limit:
        excerpt = excerpt[:limit] + "\n[... plan cut: too long ...]"
    return excerpt


# --- Bitbucket Pipelines --------------------------------------------------------------

BB_API = "https://api.bitbucket.org/2.0/repositories"


class BitbucketPipelines:
    name = "Bitbucket Pipelines"

    def __init__(self, workspace: str, slug: str, keychain: str):
        self.workspace, self.slug, self.keychain = workspace, slug, keychain
        self.base = f"{BB_API}/{workspace}/{slug}"

    def _request(self, url: str, data: dict | None = None, raw: bool = False):
        req = urllib.request.Request(url, data=json.dumps(data).encode() if data is not None else None,
                                     method="POST" if data is not None else "GET")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            auth = gitops._bitbucket_auth(self.keychain)
        except gitops.GitError as exc:
            raise CiError(str(exc)) from exc
        # not passed on to a redirect: step logs may redirect to storage that refuses it
        req.add_unredirected_header("Authorization", auth)
        try:
            with net.urlopen(req, timeout=60) as resp:
                body = resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:300]
            hint = (" (the token needs the read:pipeline scope, and write:pipeline to rerun)"
                    if exc.code == 403 else "")
            raise CiError(f"Bitbucket answered {exc.code}{hint}: {detail}") from exc
        except OSError as exc:
            raise CiError(f"Bitbucket: {exc}") from exc
        if raw:
            return body.decode("utf-8", errors="replace")
        return json.loads(body) if body else {}

    @staticmethod
    def _state(obj: dict) -> str:
        state = obj.get("state") or {}
        name = state.get("name", "")
        result = ((state.get("result") or {}).get("name") or "").upper()
        if name == "COMPLETED" or result:
            return {"SUCCESSFUL": "passed", "FAILED": "failed", "ERROR": "failed", "STOPPED": "stopped",
                    "EXPIRED": "stopped", "NOT_RUN": "skipped", "SKIPPED": "skipped"}.get(result, "failed")
        return "pending" if name == "PENDING" else "running"

    def latest(self, commit: str, pr_id: int, also: str = "") -> CiRun | None:
        """The newest pull-request pipeline for this commit, or None if none started yet."""
        url = f"{self.base}/pipelines/?sort=-created_on&pagelen=50"
        for _ in range(2):
            page = self._request(url)
            for p in page.get("values") or []:
                target = p.get("target") or {}
                if (target.get("commit") or {}).get("hash", "") != commit:
                    continue
                is_pr = ("pullrequest" in target.get("type", "") or (target.get("pullrequest") or {}).get("id") == pr_id
                         or (target.get("selector") or {}).get("type") == "pull-requests")
                if not is_pr and p.get("uuid") != also:
                    continue
                state = self._state(p)
                run = CiRun(state="running" if state in ("pending", "running") else state,
                            ref=p.get("uuid", ""), commit=commit, created=p.get("created_on", ""),
                            url=f"https://bitbucket.org/{self.workspace}/{self.slug}/pipelines/results/"
                                f"{p.get('build_number', '')}", raw=p)
                if run.done:
                    run.steps = self.steps(run.ref)
                return run
            url = page.get("next")
            if not url:
                break
        return None

    def steps(self, pipeline: str) -> list[Step]:
        page = self._request(f"{self.base}/pipelines/{pipeline}/steps/?pagelen=100")
        return [Step(name=s.get("name") or "(unnamed step)", state=self._state(s), ref=s.get("uuid", ""))
                for s in page.get("values") or []]

    def logs(self, run: CiRun, steps: list[Step]) -> None:
        for s in steps:
            try:
                s.log = self._request(f"{self.base}/pipelines/{run.ref}/steps/{s.ref}/log", raw=True)
            except CiError as exc:
                s.log = f"(the log could not be read: {exc})"

    def plan_steps(self, run: CiRun) -> list[Step]:
        return [s for s in run.steps if "plan" in s.name.lower() and s.state != "skipped"]

    def retry(self, run: CiRun) -> str:
        """Run the same pull-request pipeline again (its target, as Bitbucket gave it)."""
        target = {k: v for k, v in (run.raw.get("target") or {}).items()
                  if k in ("type", "source", "destination", "destination_commit", "commit", "pullrequest",
                           "selector", "ref_type", "ref_name")}
        return str(self._request(f"{self.base}/pipelines/", data={"target": target}).get("uuid", ""))


# --- AWS CodeBuild ----------------------------------------------------------------------

SSO_EXPIRED = re.compile(r"sso|token has expired|session.*expired|refresh failed", re.I)


class CodeBuild:
    """PR builds of a CodeBuild project. They are told apart from the project's other builds by
    who started them (`initiator`, e.g. the PR's CodePipeline: codepipeline/<name>) or, without
    one, by a webhook source version pr/<id>; and by the commit."""
    name = "AWS CodeBuild"

    def __init__(self, project: str, profile: str, region: str = "", initiator: str = ""):
        self.project, self.profile, self.region, self.initiator = project, profile, region, initiator

    def aws(self, *args: str, timeout: int = 60) -> dict:
        cmd = ["aws", *args, "--profile", self.profile, "--output", "json"]
        if self.region:
            cmd += ["--region", self.region]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError) as exc:
            raise CiError(f"aws {args[0]}: {exc}") from exc
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout).strip()[:300]
            if SSO_EXPIRED.search(err) and "AccessDenied" not in err:
                raise CiError(f"the AWS session of profile {self.profile} expired ({err})",
                              login=f"aws sso login --profile {self.profile}")
            raise CiError(f"aws {' '.join(args[:2])}: {err}")
        return json.loads(proc.stdout or "{}")

    @staticmethod
    def _state(status: str) -> str:
        return {"SUCCEEDED": "passed", "FAILED": "failed", "FAULT": "failed", "TIMED_OUT": "failed",
                "STOPPED": "stopped", "IN_PROGRESS": "running"}.get(status, "running")

    def _run(self, build: dict) -> CiRun:
        state = self._state(build.get("buildStatus", ""))
        steps = [Step(name=ph.get("phaseType", "?"), state=self._state(ph.get("phaseStatus", "IN_PROGRESS")))
                 for ph in build.get("phases") or [] if ph.get("phaseStatus")]
        region = (build.get("arn") or "::::").split(":")[3] or self.region
        url = (f"https://{region}.console.aws.amazon.com/codesuite/codebuild/projects/{self.project}/build/"
               f"{urllib.parse.quote(build.get('id', ''), safe='')}/?region={region}") if region else ""
        return CiRun(state=state, ref=build.get("id", ""), url=url, commit=build.get("resolvedSourceVersion", ""),
                     created=str(build.get("startTime", "")), steps=steps, raw=build)

    def latest(self, commit: str, pr_id: int, also: str = "") -> CiRun | None:
        """The newest PR build of the project for this commit."""
        ids = self.aws("codebuild", "list-builds-for-project", "--project-name", self.project,
                       "--sort-order", "DESCENDING", "--no-paginate").get("ids") or []
        ids = ids[:25]  # one page, newest first (the CLI refuses --sort-order while paginating)
        if not ids:
            return None
        builds = self.aws("codebuild", "batch-get-builds", "--ids", *ids).get("builds") or []
        mine = [b for b in builds if self._same_commit(b.get("resolvedSourceVersion", ""), commit)
                and self._is_pr(b, pr_id, also)]
        if not mine:
            return None
        newest = max(mine, key=lambda b: str(b.get("startTime", "")))
        return self._run(newest)

    @staticmethod
    def _same_commit(resolved: str, commit: str) -> bool:
        """CodePipeline sources resolve to an abbreviated sha (12 chars), so compare by prefix."""
        if not resolved or not commit:
            return False
        n = min(len(resolved), len(commit))
        return n >= 7 and resolved[:n].lower() == commit[:n].lower()

    def _is_pr(self, build: dict, pr_id: int, also: str) -> bool:
        if also and build.get("id") == also:
            return True
        if self.initiator:
            return str(build.get("initiator", "")) == self.initiator
        return str(build.get("sourceVersion", "")) == f"pr/{pr_id}"

    def log(self, run: CiRun) -> str:
        logs = run.raw.get("logs") or {}
        group, stream = logs.get("groupName"), logs.get("streamName")
        if not group or not stream:
            return "(this build has no CloudWatch log)"
        lines, token = [], None
        for _ in range(200):  # 10k events a page: far more than a plan prints
            args = ["logs", "get-log-events", "--log-group-name", group, "--log-stream-name", stream,
                    "--start-from-head"]
            if token:
                args += ["--next-token", token]
            page = self.aws(*args, timeout=120)
            events = page.get("events") or []
            lines += [e.get("message", "").rstrip("\n") for e in events]
            nxt = page.get("nextForwardToken")
            if not events or nxt == token:
                break
            token = nxt
        return "\n".join(lines)

    def logs(self, run: CiRun, steps: list[Step]) -> None:
        text = self.log(run)
        for s in steps:
            s.log = text

    def plan_steps(self, run: CiRun) -> list[Step]:
        return [Step(name=f"CodeBuild {self.project}", state=run.state)]

    def retry(self, run: CiRun) -> str:
        return str((self.aws("codebuild", "retry-build", "--id", run.ref).get("build") or {}).get("id", ""))

    def failed_log_steps(self, run: CiRun) -> list[Step]:
        """CodeBuild has one log for the whole build: one 'step' that holds it."""
        failed = ", ".join(s.name for s in run.failed_steps) or run.raw.get("buildStatus", "FAILED")
        return [Step(name=f"CodeBuild {self.project} (failed phase: {failed})", state="failed")]


def reader(repo, workdir_remote: str, base: str):
    """The CI reader a repo asks for, or None (pr_ci off)."""
    if repo.pr_ci == "pipelines":
        _, owner, slug = gitops.host_of(workdir_remote)
        return BitbucketPipelines(owner, slug, repo.bitbucket_keychain)
    if repo.pr_ci == "codebuild":
        return CodeBuild(repo.codebuild_project.replace("{base}", base), repo.aws_profile, repo.aws_region,
                         repo.codebuild_initiator.replace("{base}", base))
    return None


def failed_steps(ci, run: CiRun) -> list[Step]:
    if isinstance(ci, CodeBuild):
        return ci.failed_log_steps(run)
    return run.failed_steps
