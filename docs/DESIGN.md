# Design

## What I need (agreed 2026-11-17)

| Topic | Decision |
|---|---|
| Work | Code in my repos, and research/analysis |
| Repos | Personal and work; stricter rules for work repos |
| Foundation | Claude Code (headless `claude -p`, or interactive when supervised), hooks, worktrees |
| Packaging | Personal and simple: one repo, Python stdlib, no services |
| Autonomy | The goal is full, with guardrails: per-task budget, forbidden actions, approval for the irreversible, kill switch and audit. Repos start supervised (D20) and graduate to autonomous |
| Irreversible | Merge to main, deploys, talking to third parties, destructive changes |
| Jira | Moving status: In Progress by `/task run`, Code Review by the harness once the PR is ready (D23); never the task's agent. Commenting: needs approval |
| PRs | Personal: autonomous. Work: push autonomous, PR needs approval. Then the external PR review is applied (D22) and the PR's CI watched until it passes, with the IaC plan checked (D23); merging stays mine |
| Verification | Acceptance criteria with evidence + the repo's gates + one `/code-review` per code task (D21, D24) |
| Isolation | One git worktree per task on my Mac |
| Input | Jira tickets (most) and tasks planned in Claude without a ticket |
| Execution | Manual for now (no daemon), launched from Claude: supervised in a Terminal-panel tab, autonomous in the background |
| Size | Mixed: big ones get split (planner) |
| Memory | Per-repo learnings, task history, my preferences |
| Notifications | Push, per-task report, daily digest |
| Slack | A task from a Slack request replies there, formatted, only after my approval (D26) |
| Research | The report goes to Claude Docs |
| Language | Everything in the harness is in English |
| Models | Modes instead of models (ultra/smart/balanced/cheap); default smart; `!deep`/`!fast` when creating or running; effort apart with `!xhigh`/`!max`... |
| Escalation | Effort first, then model, never into ultra on its own; reruns after a failure start higher; `/task` suggests more effort when a ticket documents rework |
| Token use | Trim each agent's context; fixed steps outside the model; usage report with agent proposals you approve |
| Billing | Max subscription: dollars are list-price estimates, the quota is what runs out |

## Flow

```
 /task jira KEY ─┐                                    ┌─ needs-approval ──(approve)──┐
 /task new ──────┴─> tasks/<id>.md ─(run)─> prepare ─> agent ─> verify ─> publish ─> done
                                              │          ▲        │          │
                                       worktree+setup    └─(fix)──┘    push + PR (harness)
                                                                       work repo: PR approved
                                                                             │
              pr_review / pr_ci: wait for the external review and the CI ───┘
                 findings or CI failures ─> agent ─> gates ─> push to the same PR ─┐
                       ▲ (up to max_rounds; a transient failure is rerun once)     │
                       └──────────────────── watch the new commit's CI <───────────┘
                 CI green ─> plan check (IaC) ─> Jira to Code Review ─> done: ready for you
```

## Decisions

**D1 — On top of Claude Code, not a custom SDK loop.** `claude -p` already brings the loop,
the tools, MCP (Jira, Claude Docs), subagents, `--max-budget-usd`, `--json-schema`, `--resume`
and hooks. Tests on 2026-11-17 confirmed that a PreToolUse hook denies calls even under
`bypassPermissions`, that environment variables reach the hook, and that the claude.ai
connectors work headless. The harness only adds what is missing: queue, guard, verification,
publishing and reports.

**D2 — Guardrails only on harness runs.** The hook is injected per run with `--settings` and
does nothing without `SADYANX_GUARD_CTX`. Your interactive sessions are unchanged.

**D3 — Worktrees outside the harness repo** (`~/.sadyanx/worktrees`). Claude Code loads every
`CLAUDE.md` from the working directory up to `/`. If the worktree lived inside the harness, the
agent would inherit the harness's instructions instead of only the repo's.

**D4 — The harness pushes and opens the PR, not the agent.** So the PR only exists after
independent verification, and the Bitbucket credentials (keychain) never pass through the
agent. The agent is forbidden from `git push` and `gh pr create`.

**D5 — Approve by parking, not by waiting (autonomous runs).** Headless, nobody can answer a
permission prompt (supervised runs ask on the spot instead, D20).
The guard records the action in `runs/<id>/approvals.json`, denies it to the agent and asks it
to finish with `needs-approval`. On approval the harness resumes the same session (`--resume`)
and the guard lets that exact call through once.

**D6 — Approvable actions run alone.** The first E2E showed why: the agent put the deploy in
a chain of 10 commands joined with `&&`. The approval was bound to the whole chain and,
replayed later, it failed on an unrelated step. Now the guard denies approvable actions that
come chained and asks for them to be isolated, so what you approve is exactly the action.

**D7 — The guard reads Bash one simple command at a time.** It splits respecting quotes, drops
heredoc bodies (commit messages) except those fed to a shell, checks `$(...)` separately,
peels wrappers (`env`, `npx`, `timeout`...) and requires each rule to match at the command
position. So `grep "firebase deploy"` is not mistaken for a deploy. It also resolves the paths
named by writing commands (following `cd`) and by redirections.

**D8 — Fails closed, with honest limits.** If the guard errors, it denies. Still, it is pattern
matching: it catches what an agent does by habit or by mistake, not a deliberate attempt to
hide a write inside `python -c`. The outer layers are the worktree, the budget, the time limit,
the kill switch and the audit log. Next hardening step: the Claude Code sandbox (see open items).

**D9 — The harness decides "done", not the agent.** The agent reports criteria with evidence,
but the harness runs the gates itself, checks nothing is left uncommitted, and feeds failures
back to the agent up to `max_attempts`.

**D10 — Modes instead of models.** Taken from the idea of not picking a model per task: a
task declares how hard the work is (ultra / smart / balanced / cheap), and one table in
sadyanx.toml maps modes to model + effort. New models change one line, not every task.
`!deep` = ultra, `!fast` = balanced (cheap is not "fast" for code: a weaker model costs fix
rounds). Overrides work when creating (stored) and when running (that run only); not when
approving, since a resumed session mostly just runs the approved action. Effort can be set
apart from the mode the same way (`!xhigh`, `!max`, `--effort`), and is validated against what
`claude --effort` accepts, in the config too, so a typo fails at creation and not mid-run. The
gap it fills: a task that needs more depth than smart/high but not another model. Ultra (Fable)
is kept for complex infra planning.

**D11 — Escalate effort first, model last, never into ultra on its own.** (Replaces "only the
last attempt goes one mode up", 2026-11-17: jumping smart → ultra changed the model when what
was missing was depth.) A ladder of (mode, effort) rungs: the mode's effort up to `max`, then
the next mode from its own effort, never into a mode above `escalation_ceiling` (smart by
default; a mode you choose above it is still used). Each fix attempt of a run climbs one rung:
smart/high → smart/xhigh → smart/max. Across runs, a task that ended `failed` or `blocked`
after its agent ran gets `rework` + 1, and its next run starts that many rungs up; failures
before the agent (setup, config) do not count, and an effort you give for a run replaces the
step-up. `/task` adds the signal the harness cannot see: when a ticket documents earlier
attempts (reopened, sent back by QA or review, a history of what was tried), it writes that
history into Notes and suggests `!xhigh`/`!max` for you to confirm; it never sets it alone.

**D12 — Trim what every turn carries.** Measured first-turn context of `claude -p` (2026-11-17):

| Agent setup | Tokens per turn |
|---|---|
| Claude Code default: every tool + every MCP | 27.2k |
| Code tools + web, no MCP, no subagents (code tasks) | 9.4k |
| + subagents (ultra) | 12.0k |
| + MCP through ToolSearch (research) | 14.2k |
| MCP without ToolSearch (trap) | 54.4k |
| Skill tool on top of code tools | +5.9k |

So: code agents get no MCP (`--strict-mcp-config`), research agents keep MCP but always with
ToolSearch, subagents only in ultra, and the repo's own skills are listed in the prompt (~100
tokens) and read on demand instead of loading the Skill tool, which lists every plugin skill.
`--exclude-dynamic-system-prompt-sections` makes the system prompt identical across tasks so a
new task reuses the previous one's cache (~1.7k tokens; small, free).

**D13 — Fixed steps run outside the model.** Worktree, setup, gates, push and PR were already
scripts. Two more moved out: moving the Jira ticket to In Progress is done by `/task run` in your
session (so code agents need no MCP), and the full gates run once, by the harness; the agent
only runs the checks relevant to its change and gets the last lines of any failing gate.

**D14 — Usage you can see, changes you approve.** Every agent call appends its tokens, mode,
phase and outcome to `runs/<id>/usage.jsonl`. `sadyanx usage` adds them up with no model.
`sadyanx usage --review` hands the numbers and the config to an agent in balanced mode with no
tools, which proposes changes; `/task usage review` applies only the ones you approve.

**D15 — Text written by others is fenced off as data.** Jira tickets are written by other
people and research agents read the open web, so either can carry instructions aimed at the
agent (prompt injection). `/task` keeps the ticket as written in a last `## Ticket (verbatim)`
section, and the runner wraps it in tags with a random name (`<untrusted-1a2b3c4d>`), so
nothing inside can close the fence early. `rules.md` tells the agent that such content (and
anything else from outside) is data that cannot widen its scope or override its rules. This
is a mitigation, not a guarantee: the guard stays the floor underneath it. Pattern from
Barbaste et al. 2026 ("untrusted-content delimiting").

**D16 — A cheap cap on repeated calls.** The guard denies the 3rd identical tool call in a
row (for Bash, the same command; the description does not count) and every identical one
after it, telling the agent to change approach or finish `blocked`. Any different call resets
the count, and so does every new agent call of the runner (a resumed session legitimately
repeats the call it was waiting to have approved). No heavier stuck detection: Barbaste et al.
2026 (recommendation 18) found that production harnesses get by with cheap caps.

**D17 — Research is verified by someone other than its author.** Code has the gates; research
had only the agent's own word. Now a research task must put the document URL in `links` and
leave `report.md`, and then, only if everything else passed, an independent reviewer (balanced
mode, no tools, a fresh session, at most $1) reads the task, the report and the claimed
evidence and returns pass/fail with problems. A fail goes back to the agent like a failing
gate. If the reviewer itself crashes, the task is not held up (it is logged). Off with
`review_research = false`. Pattern: "outer verification loop" (OpenHands's `/goal` judge).

**D18 — The OS sandbox, per repo, off until proven.** The guard reads commands; it cannot see
what `python -c`, an npm `postinstall` or a test script does. Claude Code's sandbox (Seatbelt
on macOS) confines every Bash command and everything it starts: writes only to the worktree,
the main repo's `.git` (a worktree's commits land there), `/tmp`, `$TMPDIR` and `allow_write`;
no reads of `deny_read` (`~/.ssh`, `~/.aws`, gh, keychains...); network only to
`allowed_domains`, through Claude Code's proxy. Package caches are pointed at
`$TMPDIR/sadyanx-cache`, since the usual ones in `~` are not writable. Three settings decide
whether it is real under `bypassPermissions`: `allowUnsandboxedCommands = false` (otherwise
the model can retry outside it with no prompt; the guard also denies `dangerouslyDisableSandbox`),
`failIfUnavailable = true` (otherwise a sandbox that fails to start is skipped silently) and
`network.strictAllowlist = true` (otherwise unlisted domains go through with no prompt).
Setup, gates, push/PR and the guard hook stay outside it, so installs and Docker-based gates
keep working. `tests/sandbox_probe.py` proves it on this machine from the filesystem, not from
the agent's word; on 2026-11-17 (Claude Code 2.1.326) all 9 checks passed, including
`strictAllowlist` from a `--settings` file (the proxy answers 403). Off by default; turn it on
per repo with `sandbox = true` after a real task there. Barbaste et al. 2026, recommendation 10.

**D19 — A run cut off by the machine is recovered, not stuck.** If the Mac shuts down or the
runner crashes, the task used to stay `running` forever, and `run` and `clean` refuse running
tasks. Now `list`, `show`, `run` and `clean` first check the runner's pid file: if it is
missing, its process is dead, or it was written before the last boot (after a restart the
same pid can belong to another process), the task becomes `killed` with an "interrupted"
reason. If the runner died but its agent is still alive, the agent is stopped too, since
nothing would verify or record its work. `sadyanx run` then resumes the same Claude session
(`--resume` works after a reboot: the transcript is on disk) whenever the stream shows the
session had started, instead of starting over in a worktree that already holds work. While a
runner lives it holds `caffeinate -ims -w <pid>` so the Mac does not idle-sleep mid-task
(`keep_awake = false` turns it off). Timeouts use `time.monotonic()`, which on macOS does not
advance during sleep, so a sleeping Mac does not eat the task's time.

**D20 — Supervised runs first, autonomy earned per repo.** Autonomy is the goal, but trust
comes from watching runs first. A supervised run is the same task, worktree, guard and
verification, in an interactive Claude session you watch and talk to. `run_mode` decides:
`[defaults] run_mode = "supervised"`, overridable per repo in repos.toml and per run with
`sadyanx run <id> --watch / --auto` (`--fg` is the headless run in the foreground, so it means
autonomous). When a repo has had several runs with no surprises, switch it to autonomous;
what you corrected while watching becomes guard rules, prompt changes or repo memory.

- **Launch.** The harness prepares the worktree, then runs `claude` without `-p` in the
  foreground of the terminal, with the same `--settings` (guard hook), `SADYANX_GUARD_CTX`,
  model/effort from the mode and `--session-id` / `--resume`. Without a terminal, `sadyanx run`
  exits with code 3 and prints the command; `/task run` then opens it in a tab of the Claude
  app's Terminal panel, beside the conversation that launched it. Ctrl-C belongs to the
  session; the runner ignores it.
- **Questions are asked, not written down.** `rules-supervised.md` replaces "do not ask
  questions" with: ask (AskUserQuestion, added to the toolset) when a decision is genuinely
  yours; otherwise use judgment. `blocked` stays for what you cannot answer on the spot.
- **Approvals are live.** The guard context carries `interactive: true`, and a gated action
  gets `permissionDecision: "ask"` instead of being parked (D5): Claude Code prompts you right
  there. Verified on 2026-11-17 (Claude Code 2.1.326) that "ask" prompts even under
  `bypassPermissions`. Forbidden actions are still denied, approvable actions must still come
  alone (D6) so the prompt shows exactly the action, approvals and rejections recorded by an
  earlier headless run still apply, and the kill switch still denies everything (the runner
  also ends the session). The audit records "ask"; the transcript records your answer.
- **The harness still decides "done" (D9).** The agent writes its result to
  `runs/<id>/session-result.json` (that one file is added to the guard's write roots, not the
  run dir) and tells you to type `/exit`. The harness validates it against the result schema
  (headless runs get that from `--json-schema`), then runs the gates, checks nothing is
  uncommitted and publishes. A missing or invalid result offers to reopen the same session
  (not counted as an attempt); a failing gate is printed and the same session reopens with the
  fix prompt, up to `max_attempts`. Opening a work PR becomes a y/N question in the terminal
  (default no) instead of a parked approval.
- **What is lost.** `--max-budget-usd` exists only headless, so there is no dollar cap and no
  time limit: you are watching. Tokens are read back from the session transcript
  (`~/.claude/projects/*/<session>.jsonl`, from where this session started); the dollar
  estimate is not available, so usage shows $0 for these calls.
- **Folder trust.** Interactive `claude` asks "Do you trust this folder?" (default "No, exit")
  on a folder it has not seen. Observed on 2026-11-17: a git worktree is trusted through its
  main repo's entry only (a trusted parent does not count), and a non-git folder through itself
  or any parent. So code tasks need each registered repo trusted (yours already were), research
  workspaces need `~/.sadyanx`. `sadyanx install` writes exactly those entries
  (`projects[path].hasTrustDialogAccepted` in `~/.claude.json`, atomically, nothing else);
  `sadyanx doctor` checks them; the runner warns before a session that will ask.
- **Rejected: a Claude desktop session.** The harness cannot start one, and could not pass
  the guard's environment to it; a Terminal-panel tab is inside the same app anyway.
- **Tested** with a real Haiku session driven through a pseudo-terminal: guard "ask" →
  approved → result file → `/exit` → gates → push. And a runner killed mid-session came back
  as `killed` (D19) and resumed the same session.

**D21 — Every code task gets Claude Code's `/code-review` before it is published.** Once the
gates pass and nothing is uncommitted (reviewing code that fails its gates would review it
twice), the harness runs `/code-review <level> <origin/base>` in the worktree: a fresh headless
session (`code_review_mode`, smart by default), read-only tools (`Bash, Read, Grep, Glob, Agent,
ReportFindings`), no MCP, its own guard context (`guard-review.json`, `deny_gated`: approvable
actions are denied outright, and an approval meant for the agent is never spent). Level: `[defaults]
code_review = "medium"`, per repo `code_review`, `off` to skip.
- **Findings go back to the agent** for one round, and the review is not run again (D24,
  which replaced "the next attempt is reviewed again"). If the review comes at the last attempt
  (the gates passed only then), the task is published anyway, with its findings in the PR body
  and the report for a human to judge: a reviewer can be wrong, and the gates are the hard bar.
- **Reading the findings.** Launched from the harness (no parent Claude session), Claude Code
  runs `/code-review` as a local command: it does its own review and returns only its final
  text (`local_command: code_review` in the result, 0 main-loop turns), so neither its
  `ReportFindings` tool nor `--json-schema` reach the stream. (From inside a Claude session the
  skill expands into the conversation instead and does call ReportFindings; do not test it
  from there.) So the harness takes the structured output or a ReportFindings call if present,
  and otherwise extracts the findings from that text with a no-tools session (balanced, schema,
  the text fenced as data, "do not add, drop, merge or judge"). Measured 2026-11-17 on a 2-bug
  diff: Opus review ~$0.03-0.16, extraction ~$0.025, all 5 findings carried over intact. If even
  that fails, the task is not held up, but the review's text goes to the report.
- Each review leaves `code-review-<attempt>.json` (findings and full text) and its raw stream;
  usage records phases `code-review` and `review-extract`. In supervised runs it runs between
  `/exit` and publishing, and its findings reopen the session like a failing gate.

**D24 — One `/code-review` per task, and one round for its findings.** Task 0004 (2026-11-18)
showed re-reviewing does not converge: three reviews of the same branch gave 7, 8 and 7 findings,
mostly new each time and increasingly nitpicky (a plural title, a recovery emoji), and each fix
round cost more than the last (0.6M, then 1.6M tokens), plus ~$0.60 per review and extraction.
- The review runs the first time the gates pass, once per task: `code_reviewed` in the task
  records it, so a rerun after a failure does not review again either.
- Its findings get their own prompt (`prompts/code-review.md`, phase `review-fix`), framed like
  the external PR review: check each one, fix what holds up, leave what is wrong, taste or out
  of scope, one line per finding in `summary`. It says the review will not be repeated, so the
  agent's judgment is final. That answer is kept in `code-review-<attempt>.json` (`answer`) and
  shown in the report under the findings.
- After that round only the gates and criteria are checked (gate failures still get fix rounds).
  The external PR review (D22) is the next look at the code.

**D22 — After the PR opens, the task's agent applies the external PR review.** On my work
repos an external agent posts a review as a PR comment, `## Automated PR Review` (Security and
Code Quality tables, a verdict such as `PROBLEMS_FOUND`, `PASSED_WITH_NOTES`, `NEEDS_ATTENTION`).
Repos with `pr_review = true` (Bitbucket only) wait for it and act on it.
- **Waiting.** The runner reads the PR's comments right away and then every `poll_s` (60) for
  up to `wait_min` (10); measured 2026-11-18, the review lands 45 s to 2 min after the PR
  opens. The comment is matched by its first line (`marker`), not its author: it is posted
  with your own account. Top-level, not inline, not a reply, not deleted; the latest wins.
  Network errors keep it polling. No review in time ends the task `done`, saying so; a review
  with no findings table also ends it without calling the agent. Waiting is not agent time:
  it does not count against `timeout_min`.
- **One round.** Across 40 recent work PRs, none had more than one review, though many got
  later pushes: it reviews a PR once. So the harness does not wait again after its push.
- **The fixes.** The same agent session resumes (it knows the change) with the review fenced as
  data, and goes through every finding: fix and commit the ones that hold up, leave the rest,
  and give one line per finding in `summary` (applied: what; not applied: why). Then the
  normal attempts loop: gates, uncommitted changes and criteria, up to `max_attempts`, but no
  `/code-review` (the external review already covers the branch). The harness pushes the new
  commits to the same branch, so to the same PR; nothing new → nothing pushed. It does not
  reply on the PR: that would be commenting, which is approvable.
- **Failing fixes are not pushed.** The PR keeps the verified code; the task ends `failed` with
  phase `pr-review`, so `sadyanx run` goes back to the review (found at once) and retries.
- **Getting there.** Personal repos and supervised work repos: same runner, right after the PR
  opens (supervised: the session reopens in the same terminal). Autonomous work repos: the PR
  waits for approval; `sadyanx approve` opens it, sets phase `pr-review` and starts a run in the
  background. A runner that dies while waiting is recovered like any other (D19) and its rerun
  waits again. `pr-review.md` keeps the comment, `pr-review.json` its verdict, link and
  outcome; the report gets an "External PR review" section; usage records phase `pr-review`.
- **HTTPS.** The python.org Python ships without CA certificates, so `urllib` failed TLS before
  sending anything; `sadyanxlib/net.py` uses the system bundle (`/etc/ssl/cert.pem`) when Python
  has none, and never turns verification off. `sadyanx doctor` shows which one is used.

**D23 — The harness watches the PR's CI until the PR is ready for my review.** Opening a PR runs
the repo's validation pipeline: lint, typecheck, tests, audit, standards and secret scans on the
app repos; fmt/validate/tflint plus a terraform plan on the IaC repos. The harness takes the PR
to the point where only my review and merge are left: CI green and, on IaC, a plan that is the
change the task asked for. Repos opt in with `pr_ci`; `[pr_ci]` in `sadyanx.toml` holds the
limits. Decided with me 2026-11-18:
- **Where it reads the CI (the harness, never the agent).** `pipelines`: Bitbucket Pipelines
  through the Bitbucket API with the repo's keychain token, which needs `read:pipeline` (logs)
  and `write:pipeline` (rerun). The PR's run is the newest pull-request pipeline whose commit is
  the PR's head. `codebuild` (an IaC repo whose CI does not report to Bitbucket at all): the aws
  CLI with my profile (`aws_profile`). Its PR builds run in the same project as its merge
  builds (`<app>-build-<base>`), started by a separate CodePipeline
  (`<app>-pr-<base>`): they are told apart from merge builds of the same project
  by `initiator` (`codebuild_initiator`) and by commit (checked on real builds 2026-11-18). The
  `--sort-order` listing must use `--no-paginate`. An expired SSO session notifies me once with
  the `aws sso login` command and keeps polling; still expired at the end → `blocked`.
- **One wait for both.** After the PR opens, the runner polls the review comment (D22) and the CI
  together (`poll_s`). The CI must start within `start_min` (10) and finish within `wait_min`
  (30; the PR pipelines take under 15). Either missing → `blocked`, phase `pr-review`, and
  `sadyanx run` waits again. If the review is in and the CI is late, the review round goes first.
- **One round for both.** Review findings and the failed steps' logs (ANSI stripped, the last
  60k characters of each, fenced as `CI log` data; whole logs kept in `runs/<id>/ci-<n>-failed/`)
  go to the same agent session in one prompt, phase `pr-review` (`ci-fix` when there is no
  review). Same attempts loop as D22: gates, criteria, no `/code-review`; only verified commits
  are pushed. Then the new commit's CI is watched again. A review round does not count as a CI
  round; at most `max_rounds` (3) CI rounds, then `blocked` with the agent's last diagnosis.
- **Failures outside the change.** The agent classifies them in `ci_cause`: `change` (it
  committed fixes), `transient` (flaky test, runner, network, a token the pipeline failed to
  fetch) or `outside` (a new CVE in an untouched dependency, a secret-scan false positive, a
  broken shared pipeline). It does not change code for the last two. Transient with no commits →
  the harness reruns the run once (Bitbucket: POST the same pull-request target; CodeBuild:
  `retry-build` on that PR build only, which I allowed; the rerun is found by its id, since a
  retried CodeBuild build is not started by the pipeline). Failing again, `outside`, or `change`
  with nothing committed → `blocked` with the diagnosis, for me. None of these count as rework.
- **App repos: green is enough.** No extra checks of warnings or coverage (my call).
- **IaC: the plan check (`plan_check`).** Once the CI passes, an independent check (balanced
  mode, no tools, fresh session, like the research review) gets the task, the diff and the plan
  steps' logs (the `plan` steps on Pipelines, the build log on CodeBuild; trimmed to the
  `Terraform will perform...` → `Plan:` blocks when long). It lists every resource change with
  why, flags as `unexpected` what the diff and task do not explain (any destroy/replace not
  asked for, a missing plan where the diff should give one) and as `drift` what changed outside
  Terraform. Unexpected → `blocked`, nothing is "fixed"; drift alone is only reported; a check
  that fails gives no verdict → `blocked` (fail closed). No resource type stops it by itself.
  A CI that plans only some environments (e.g. the PR build into `develop` plans `test`, and
  `prod` is planned only on `master`) declares them in the repo's `plan_envs`. An environment
  outside that list that the diff changes goes in `unplanned` (what to check in its own plan):
  shown in the report and the ready note, not blocking. The harness keeps an `unplanned` entry
  only for an environment the config says the CI does not plan; without `plan_envs`, or for a
  planned environment, it becomes `unexpected` (fail closed). A change the diff explains is not
  unexpected because the task named another environment (a shared module changes them all).
  After a review round the plan is checked on the new commit only.
- **Ready.** The ticket moves to `ready_jira_status` (`CODE REVIEW` by default, through whatever
  transition the issue offers into it) in a short headless session in cheap mode whose only
  tools are ToolSearch and the MCP servers, under the guard with nothing approvable (moving a
  status is allowed, commenting is not). A failed move is noted, not fatal. Then `done`: "PR ready
  for your review: ... passed, plan as expected, KEY in CODE REVIEW", notified. Nothing is posted
  on the PR. `ci.json` keeps every run, `plan-check.json` and `plan.txt` the plan; the report
  gets a "PR CI" section; usage records `ci-fix`, `plan-check` and `jira`.
- **Supervised.** Like D22: the tab waits and the session reopens there for each round.
- **Not yet.** GitHub Actions (a personal repo) comes later.

**D25 — The project is sadyanx, public on GitHub; my setup stays on my machine.** Renamed from
the working name "harness" (2026-11-18): command `sadyanx`, package `sadyanxlib`, state in
`~/.sadyanx`, variables `SADYANX_*`, config `config/sadyanx.toml`. "The harness" stays as the
plain word for its role in prompts and docs. The repo is public, so what is tracked is only the
code, prompts, docs and generic examples: `tasks/`, `runs/` and the two config files are ignored,
and the repo ships `config/sadyanx.example.toml` (also the fallback when there is no
`sadyanx.toml`) and `config/repos.example.toml`, which `sadyanx install` copies into place. Docs and
tests use generic names (PROJ-1234, work-api); the real measurements behind D22-D24 keep their
numbers but not the employer's names.

**D26 — A reply on Slack to whoever asked, sent by the harness after my yes.** Many requests
arrive as a Slack message, and the answer goes back there. Decided with me 2026-11-26:
- **Where it goes: only where the request came from.** `/task` passes the message's link
  (`sadyanx add --slack <link>`); the task keeps `slack_channel` and `slack_thread`. In a direct
  message the reply is a plain message in the same conversation (or in the thread, if the request
  was in one); in a channel it is always a reply in the request's thread. No other recipient.
- **Who writes what.** The agent writes only the body, in the result's `reply` field, in
  `[slack] language` (prompt `reply.md`, added to the first prompt only for these tasks). The
  harness adds the format: `greeting`, the body, `sign_off` (mine: "Hola!" ... "Saludos."),
  dropping a greeting or sign-off the agent wrote anyway. Empty or over `max_chars` → not sent,
  and said so in the reason. Only a task that ends `done` replies.
- **Who sends it: the harness, on my yes.** Supervised: the rendered message is printed in the
  tab with "Send this reply on Slack? [y/N]". Otherwise it is parked as a harness approval
  (`slack-reply`, phase `reply`) whose description is the whole message, so `/task approve` shows
  exactly what goes out. Rejecting sends nothing and ends the task `done`.
- **How it is sent.** A short headless session (cheap mode, `ToolSearch` and the MCP servers only,
  no session persistence, a temp dir as cwd, $0.30 cap) with its own guard context: nothing
  writable, nothing approvable (`deny_gated`), and one **pinned call**: `[slack] send_tool` with
  exactly the approved channel, thread and text (keys left empty count as absent). Any other
  text, channel or Slack write is denied, and a second identical send is cut off by the repeat
  cap. The harness counts it as sent only if the session says so **and** the audit shows the
  guard let the pinned call through. A failure does not undo the task: the text stays in
  `runs/<id>/slack-reply.md` to send by hand. Usage records phase `slack`.
- **Agents never send Slack messages.** `slack_send_message` and `slack_schedule_message` are
  denied to every agent (not gated, not allowable with `mcp_allow`): a reply would skip the format
  and the review. Other Slack writes (reactions, canvases, drafts, uploads) are gated as usual.
- **Fixed on the way: prefixed MCP operations.** The write check matched the operation's first
  word, and the Slack connector names every operation `slack_<verb>_...`, so Slack writes were
  allowed with no approval. Now an operation is also checked without its first word, unless it
  starts with a read verb (`list_merge_requests` stays a read), and `complete` counts as a write
  (`slack_complete_file_upload` posts a file).
- **Rejected: a Slack token in the keychain and the Web API.** It would post as a bot (or need a
  user token I would have to mint and guard); the claude.ai connector already posts as me, like
  the Jira move in D23.
- **Tested** 2026-11-26: the send session posted to my own DM in 8 s for $0.003 (Haiku); the
  tool is `mcp__claude_ai_Slack__slack_send_message`, and the audit shows `ToolSearch`, the pinned
  send and the result, nothing else. Not yet: a real task drafting its own reply.

**D27 — A desktop dashboard that reaches the harness through its own MCP server.** The terminal
answers one question at a time; I want to see the queue, what waits for me and where the tokens
go at a glance. `app/dashboard.html` is a claude.ai artifact: installed from the Claude desktop
app it is a Mac app with its own window, Dock badge and an optional menu bar item showing how
many approvals wait.
- **Through `sadyanx mcp`, not a shell.** v1 used the `desktop` capability's full access (run
  any program) to call the CLI. My organization does not allow full access for artifacts, for
  good reason: a page that can run anything is a large door. Now `sadyanx mcp` is a local stdio
  MCP server, registered once in the Claude desktop app's `claude_desktop_config.json` (the installed
  app does not see Claude Code's `~/.claude.json`), and the page declares only `host:sadyanx` with four tools: `status`
  and `report` (read-only), `approve` and `reject`. That is all the page can reach. Each call runs the CLI in
  a fresh process with an argument list (no shell), so the CLI stays the single path to an
  approval and code changes apply without restarting the server. Ids are checked against their
  shape, `approve` never takes `--all`, and the Claude app may ask me to confirm the two writes.
- **One source of truth.** `status` is `sadyanx status --json --days N`: the queue, pending
  approvals, the kill switch, the guard's verdicts (from `audit.jsonl`: totals, categories of
  what it stopped, the latest denials) and `usage.summarize`, which now also has `by_day`, agent
  seconds and usage by model, by effort and by model/effort (effort alone mixes models). Like `list`, it first marks a task whose runner is gone as `killed`.
- **Work done without a model, counted, never estimated.** `status` counts from `events.log`
  the steps the harness does itself (gate runs, setup commands, verifications, pushes, PRs
  opened, CI results read, CI reruns). Tokens not spent cannot be measured, so the dashboard
  shows counts, not a "tokens saved" figure built on guesses. Steps that use a model (reviews,
  plan check, Jira, Slack) stay in the usage numbers.
- **Reports in the app.** `report` returns `runs/<task>/report.md` and nothing else: the id is
  checked, the resolved path must be that file (a link out of `runs/` is refused) and it is cut
  at 256 KB. The page renders it with its own small Markdown renderer that escapes everything
  first and only links to https.
- **Approve and reject, nothing else.** Each pending action shows its full text (a Slack reply
  in full) with Approve… and Reject…; the second click, inside the card, calls the tool once
  for the exact approval id it showed, so an action decided meanwhile is refused, never
  re-decided. A lost answer is not retried: it may have run, so the page says to check the queue.
- **Agents never reach it.** Registered for the user, the server is also loaded in agents'
  sessions. The guard denies every tool of a server whose name contains `sadyanx`, before
  `mcp_allow` and pins (`SADYANX_MCP_TOOL`), and the server refuses every call when its
  environment shows a sadyanx agent session (`SADYANX_GUARD_CTX`, `SADYANX_TASK_ID`).
- **Found on the way, both fixed.** `approve` carried out harness actions (push, open the PR,
  send on Slack) with the kill switch on; now `approve` and `reject` refuse until `unkill`. And
  an agent could run `sadyanx approve` on its own task through Bash (the CLI writes the approval
  file itself, so no write rule saw it); now the CLI, `python bin/sadyanx` and Python importing
  `sadyanxlib` are forbidden to agents. Python code an agent writes could still reach the
  approval files outside the sandbox: see Known limits.
- **Nothing of mine in the page.** The HTML is tracked in this public repo and carries no data:
  everything comes from `status` at run time. Outside the Claude desktop app (a browser) it
  shows marked example data; an artifact keeps its owner-only, unshared access, since host
  servers answer only its owner.
- **Rejected:** full access (above), a local web server (another process to keep alive, a port
  to protect) and the `db` capability (it would copy my tasks and usage to a server for no gain).

**D28 — A persona, only where sadyanx talks to me.** The harness should feel like someone, not
like a log. `config/persona.toml` (local; `persona.example.toml` ships and `install` copies it)
gives it a name, a voice and lines per event.
- **Where it speaks:** the subtitle of notifications (the title and message keep the facts), the
  first line of `sadyanx status`, the dashboard, and `/task`, which runs `sadyanx persona` and
  talks to me in that voice and language.
- **Where it never speaks:** agents' prompts (they stay terse, and a persona there would cost
  tokens on every turn and loosen their discipline), and anything other people read: Slack
  replies keep their `[slack]` format, and PRs, Jira and task files stay plain English.
- **Templates, not a model.** Lines are picked at random and filled with `string.Template`
  (`$id`, `$title`, `$reason`, `$pr`, `$n`): no tokens, no latency, and nothing a placeholder can
  reach beyond the values given. Lines are cut at 160 characters.
- **It never breaks anything.** A missing, disabled, nameless or unparsable file means no persona;
  a line adds to the facts and never replaces them.
- **Next:** the daily digest (Iteration 2) is where a model will write in this voice.

## Known limits

- The guard reads commands, not what code does: an agent's own Python script could still edit
  `runs/<id>/approvals.json` when the sandbox is off. The sandbox (D18) is what closes that.

- `caffeinate` does not stop a laptop from sleeping when its lid is closed on battery. The run
  pauses until it wakes; if the network calls time out meanwhile the agent may fail, and
  `sadyanx run` resumes it.
- Killing a task does not record that run's dollar estimate (Claude never emits its result);
  its tokens are still recorded from the stream.
- The claude.ai sign-in expires (the warning said between 2026-11-21 and 2026-11-24). For long
  unattended runs use `claude setup-token` and export `CLAUDE_CODE_OAUTH_TOKEN`.
- `make setup` on my work repos needs valid CodeArtifact auth. If it fails, the task ends
  `failed` with `runs/<id>/setup.log`.
- Inside the sandbox, `gh` (its config is unreadable, and Go TLS may fail), Docker and git over
  SSH do not work, and only allowlisted domains are reachable. A task that needs more ends
  `blocked` saying what; add the domain or path to the repo's `sandbox_domains` /
  `sandbox_allow_write`. Work repos will need their CodeArtifact domain before turning it on.
- The research reviewer has no tools: it cannot open the Claude Docs document, only
  `report.md`, so it does not catch a document that differs from its Markdown copy.
- Tested end to end: code task → gates → push, approval → resume, kill; on a real work task,
  opening the Bitbucket PR and the external PR review round (2026-11-18). The CodeBuild reader
  was checked against real PR builds (finding the build, its log and plan). **Not
  yet tested against real services**: opening a PR on GitHub, the Bitbucket Pipelines API (the
  token lacked `read:pipeline` when it was written), both reruns, the Jira move to Code Review,
  the research report in Claude Docs, and a real task's Slack reply (D26; the send itself was
  tested). Watch the first real task of each kind.

## Iteration 2

1. **Planner**: `/task plan <id>` splits a big task into subtasks (`parent`, `depends_on`) with
   their own criteria; `run` honors the dependencies.
2. **Memory**:
   - `memory/repos/<repo>.md`: learnings curated from each result's `learnings` field.
   - `memory/preferences.md`: your working preferences.
   - Both get injected into the agent prompt. History already lives in `tasks/` and
     `runs/*/report.md`; it still needs to be searchable.
3. **Daily digest**: `sadyanx digest`, plus a launchd job that sends it as a notification or document.
4. **Optional daemon**: launchd picking up `ready` tasks, N in parallel.
5. ~~Claude Code sandbox~~: built (D18), off by default. Next: turn it on for a personal repo,
   run a real task, then the work repos once their CodeArtifact domain is allowlisted.
