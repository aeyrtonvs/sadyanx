# sadyanx

Let Claude Code agents work on your tickets on their own, and get back a PR that is ready for
your review: tested, reviewed, with its CI green, and with evidence for every acceptance
criterion. Anything irreversible waits for your yes.

sadyanx is a personal, local harness around [Claude Code](https://docs.claude.com/en/docs/claude-code).
You give it a task (a Jira ticket, or something you planned in a Claude conversation); it runs
an agent in an isolated git worktree under guardrails, verifies the work itself, pushes the
branch, opens the PR, applies the review it gets and watches the PR's CI until it passes.
You drive it from Claude Code with a `/task` command.

## What it is

- **A task queue and a runner on your Mac.** Tasks are Markdown files; each run gets its own
  worktree and branch, so agents never touch your checkout and several tasks can sit side by side.
- **Guardrails on every tool call.** A Claude Code `PreToolUse` hook checks each action: some are
  forbidden (pushing, reading secrets, writing outside the worktree), some need your approval
  (deploys, migrations, commenting on PRs or tickets), the rest runs. It fails closed. A kill
  switch stops everything.
- **Verification that is not the agent's word.** The repo's own gates (lint, tests...) must pass,
  nothing may be left uncommitted, each acceptance criterion needs evidence, and code gets one
  `/code-review`. Failures go back to the same agent session.
- **The whole PR loop.** sadyanx (never the agent) pushes and opens the PR, waits for an external
  review comment if your team has a review bot, watches the PR's CI (Bitbucket Pipelines or AWS
  CodeBuild), sends failures back to the agent, checks Terraform plans against the task, and
  moves the Jira ticket to Code Review when the PR is ready.
- **Supervised or autonomous.** Watch the agent in a terminal and answer it, or let it run headless
  with a budget and a time limit and get notified.
- **Token-aware.** Tasks pick a mode (ultra/smart/balanced/cheap) instead of a model, escalate
  effort only when an attempt fails, and every agent call's tokens are recorded so
  `sadyanx usage` can show where they go.

## What it is not

- **Not a service or a CI.** Nothing runs in the cloud or on a schedule: it runs on your Mac when
  you start a task. There is no daemon, no web UI, no team setup.
- **Not a merge bot.** It never merges and never deploys on its own. The PR ends ready for *your*
  review; merging is yours.
- **Not a replacement for review.** It reviews and checks a lot, but a human reads the PR.
- **Not an agent framework.** It does not call the Claude API or define agents: it drives the
  `claude` CLI you already use, with its own prompts, hook and settings.
- **Not cross-platform.** It uses the macOS keychain, notifications and `caffeinate`.
- **Not every forge.** PRs open on GitHub (`gh`) and Bitbucket Cloud. The PR review and CI loop
  is Bitbucket Cloud only for now (GitHub Actions is next). Bitbucket Server/Data Center is not
  supported.
- **Not free to run.** Agents use your Claude plan. On a Max subscription the dollar figures are
  list-price estimates; the usage quota is what runs out.

## Requirements

| What | Needed for |
|---|---|
| macOS, Python 3.11+ (standard library only), git | everything |
| [Claude Code](https://docs.claude.com/en/docs/claude-code), signed in | the agents and the `/task` command |
| `gh`, signed in | opening PRs on GitHub |
| A Bitbucket API token in the keychain | opening PRs on Bitbucket, reading reviews and CI |
| The Atlassian connector in Claude | `/task jira`, and moving tickets |
| The aws CLI and a profile | only for repos whose PR checks run on AWS CodeBuild |
| The Claude Docs connector | only for research tasks (their report goes there) |

## Setup, with Claude doing the typing

Most of the setup is editing two TOML files, and Claude Code is good at that. The only things
you have to do yourself are the ones that involve your credentials.

### 1. Clone and install

```sh
git clone https://github.com/aeyrtonvs/sadyanx.git ~/code/sadyanx
cd ~/code/sadyanx
bin/sadyanx install
```

`install` creates your `config/sadyanx.toml` and `config/repos.toml` from the examples, links the
`sadyanx` command into `~/.local/bin` (put it on your `PATH`) and the `/task` command into
`~/.claude/commands`, and marks your registered repos as trusted in Claude Code. Your config,
the task queue (`tasks/`) and the runs (`runs/`) are ignored by git: they stay on your machine.

### 2. Let Claude register your repos

Open Claude Code in the sadyanx folder and describe your repos. For example:

> Register my repos in `config/repos.toml`, following `config/repos.example.toml`:
> - `~/code/my-app` is personal, on GitHub, base `main`; its checks are `npm run lint` and
>   `npm test`, setup is `npm ci`.
> - `~/work/api` is a work repo on Bitbucket, base `develop`. Read its Makefile and its
>   `bitbucket-pipelines.yml` to pick the setup and gates, use the keychain item
>   `bitbucket-work`, and watch its PR CI (`pr_ci = "pipelines"`).
> - `~/work/infra` is Terraform on Bitbucket whose PR checks run on AWS CodeBuild. Find which
>   CodeBuild project and pipeline run its PR builds with my `ci-reader` AWS profile (read-only
>   commands), and turn on the plan check.
>
> Then run `sadyanx doctor` and fix what it reports.

Things worth knowing when you (or Claude) fill a repo in:

- `kind = "work"` makes opening the PR wait for your approval; `personal` opens it on its own.
- `gates` are the commands that decide whether the work is done. Prefer the ones the CI runs, so
  a green run here means a green PR.
- `branch` takes `{id}`, `{jira}` and `{slug}`: e.g. `feature/{jira}-{slug}`.
- `run_mode = "supervised"` is the default. Move a repo to `autonomous` once its runs bring no
  surprises.
- The comments at the top of `config/repos.example.toml` and `config/sadyanx.example.toml`
  document every key.

### 3. The credentials only you can create

Claude can tell you what to click and run the commands that store things, but the tokens and
logins are yours. Never paste a token into the chat.

**Bitbucket API token** (PRs on Bitbucket, the review comment, the CI). At
id.atlassian.com → Security → *Create API token with scopes* → Bitbucket, give it:

| Scope | For |
|---|---|
| `read:repository:bitbucket`, `read:project:bitbucket` | the repo |
| `read:pullrequest:bitbucket`, `write:pullrequest:bitbucket` | reading the PR and its comments, opening it |
| `read:pipeline:bitbucket` | the PR's CI: its steps and logs |
| `write:pipeline:bitbucket` | rerunning a CI run that failed for a transient reason |

Copy the token and store it with your Atlassian email as the account (the item name is the
`bitbucket_keychain` of your repos):

```sh
security add-generic-password -U -s bitbucket-work -a you@example.com -w "$(pbpaste)"
```

Use `-w "$(pbpaste)"`: typing the token at the interactive `-w` prompt silently cuts it to 128
characters, and these tokens are longer. Scopes cannot be added to an existing token; create a
new one. Pushing does not use the token: it goes through your usual git remote (SSH).

**Atlassian connector** (`/task jira`, moving tickets). Add the Atlassian connector to Claude
(claude.ai → Settings → Connectors) and sign in. The guard allows moving a ticket's status and
asks before commenting.

**AWS** (only for CodeBuild CI). A profile that can run `codebuild:ListBuildsForProject`,
`codebuild:BatchGetBuilds`, `codebuild:RetryBuild` and `logs:GetLogEvents`. With SSO, log in
before a run (`aws sso login --profile ci-reader`); if the session expires while a task waits
on CI, sadyanx notifies you with that command and keeps polling.

**GitHub** (only for PRs on GitHub): `gh auth login`.

**Long unattended runs**: the Claude sign-in expires; for autonomous runs that must not stop,
create a long-lived token with `claude setup-token` and export `CLAUDE_CODE_OAUTH_TOKEN`.

**Phone notifications** (optional): install the ntfy app, subscribe to a hard-to-guess topic and
put it in `ntfy_topic` in `config/sadyanx.toml`. macOS notifications work out of the box.

### 4. Check it

```sh
sadyanx doctor
```

It checks Claude Code, `gh`, the config, each repo (base branch, trust, keychain item, PR review
and CI settings, the AWS profile) and the TLS certificates. The python.org Python ships without
CA certificates; sadyanx then uses the system bundle, and `doctor` says so.

Then, in any Claude Code session: `/task jira PROJ-1234` (or `/task new` after planning
something), and `/task run <id>`.

## Day to day (from Claude Code)

| You want | Type |
|---|---|
| A task from a ticket | `/task jira PROJ-1234` (optionally `/task jira PROJ-1234 my-backend`) |
| A task from what you just planned | `/task new` |
| A task from a Slack request | `/task new` with the message's link: the agent drafts a reply and the harness sends it there after your yes |
| Run it | `/task run 7` — moves the Jira ticket to In Progress, then opens the session in a Terminal-panel tab (supervised) or runs it in the background and notifies you (autonomous) |
| Choose for one run | `/task run 7 watch` (supervised) · `/task run 7 auto` (autonomous) |
| Pick how hard the work is | add `!deep` (ultra) or `!fast` (balanced) to `jira`, `new` or `run` |
| See where the tokens go | `/task usage` · `/task usage review` (an agent proposes savings; you approve) |
| See the queue | `/task` |
| Approve / reject what is pending | `/task approve 7` · `/task reject 7 <id> not on a Friday` |
| Stop one task / everything | `/task kill 7` · `/task kill` |

The same is available as a CLI (`sadyanx --help`): `add`, `list`, `show`, `run`, `approve`,
`reject`, `kill`, `unkill`, `log`, `clean`, `usage`, `repos`, `doctor`, `install`.

## Supervised or autonomous

| | Supervised (default) | Autonomous |
|---|---|---|
| Where | an interactive Claude session in a terminal (the Terminal panel) | headless `claude -p`, in the background |
| Questions | the agent asks you on the spot | it decides, or ends `blocked` |
| Irreversible actions | Claude Code asks you to approve them right there | parked as `needs-approval`; `/task approve` resumes |
| When it is done | the agent says so and you type `/exit`; the harness verifies and publishes | the harness verifies and publishes, then notifies you |
| Limits | you are watching (no dollar or time cap) | budget and time limit per task |

`run_mode` in `config/sadyanx.toml` sets the default; set it per repo in `config/repos.toml`
and switch a repo to `autonomous` once its runs bring no surprises. `sadyanx install`
marks the repos as trusted so Claude Code does not ask about each new folder.

## Modes, not models

A task says how hard the work is; `config/sadyanx.toml` says who does it.

| Mode | Model / effort | For | Shortcut |
|---|---|---|---|
| ultra | Fable / xhigh, with subagents | complex infra planning; only when you choose it | `!deep` |
| smart | Opus / high | everyday work (default) | |
| balanced | Sonnet / medium | routine, high-volume work | `!fast` |
| cheap | Haiku / low | quick checks | |

- A mode given when creating a task is stored in it; one given to `run` applies to that run only.
- **Effort** apart from the mode: `!low` `!medium` `!high` `!xhigh` `!max` (e.g. `/task jira
  PROJ-1234 !xhigh` for a task that needs more depth than smart/high but not Fable).
- **Escalation**, effort first and model last: each fix attempt of a run goes one step up
  (smart/high → smart/xhigh → smart/max), never into ultra on its own (`escalation_ceiling`).
- **Reruns** of a task that failed or was blocked start one step higher per earlier failure,
  unless you give an effort for that run.
- **`/task jira`** suggests `!xhigh`/`!max` when the ticket documents earlier attempts (reopened,
  sent back by QA or review), and writes that history into the task's Notes. You confirm.
- The mode also sets the toolset. Code agents get only code tools + web, with no MCP (~9.4k
  tokens of context per turn instead of ~27k); research agents add MCP through ToolSearch
  (~14k). Subagents only in ultra. A repo's own skills are listed in the prompt and read on
  demand instead of loading the Skill tool.

## What happens when a task runs

1. **Prepare** a worktree at `~/.sadyanx/worktrees/<id>` on its own branch from the repo's base,
   and run the repo's `setup` (install dependencies).
2. **Agent**: in the task's mode, with the sadyanx guardrails and a structured result (status,
   criteria with evidence, PR title/body). Autonomous: `claude -p` with a budget
   (`--max-budget-usd`) and a time limit. Supervised: interactive `claude` you watch; it writes
   the result to a file and you type `/exit`. The agent runs only the checks relevant to its change.
3. **Verify**: the harness runs the repo's full `gates`, which must pass, nothing may be left uncommitted and every
   criterion must be met with evidence. Then, on code tasks, Claude Code's `/code-review`
   (`code_review = "medium"`, per repo too, `off` to skip) reviews the branch once per task: the
   agent goes through its findings in one round (fixes what holds up, says why it leaves the
   rest) and the review is not run again; the external PR review is the next look. Research tasks must leave the document link and
   `report.md`, and an independent reviewer (balanced mode, no tools) checks the report against
   the criteria. Failures go back to the same agent (up to `max_attempts`).
4. **Publish** (done by the harness, not the agent): push the branch and open the PR. On
   `personal` repos the PR opens on its own; on `work` repos it waits for your approval and the
   harness opens it on Bitbucket with the credentials in the keychain.
5. **External PR review** (repos with `pr_review = true`, Bitbucket): the harness checks the PR
   every minute for up to 10 for the review comment another agent posts (`## Automated PR
   Review`). When it arrives, the same agent goes through each finding, fixes the ones that hold
   up and says why it left the rest; the gates run again and the harness pushes to the same
   PR. Fixes that fail the gates are not pushed. No review in 10 minutes: the task goes on
   anyway. Settings in `[pr_review]` of `sadyanx.toml`.
6. **PR CI** (repos with `pr_ci`): at the same time it watches the PR's pipeline (Bitbucket
   Pipelines, or AWS CodeBuild for repos whose CI runs there). Failed steps' logs go to the same agent, in
   the same round as the review: it fixes what the change causes and the harness pushes, then
   watches the new commit's CI, up to 3 rounds. A failure outside the change is rerun once if
   it looks transient, otherwise handed to you. On IaC repos (`plan_check`) an independent
   check reads the plan and stops on anything the task does not explain; environments the CI
   does not plan (`plan_envs` in the repo) are listed for your review instead. When it is green, the
   Jira ticket moves to Code Review and you get "PR ready for your review"; the merge is yours.
   Settings in `[pr_ci]` of `sadyanx.toml`.
7. **Slack reply** (tasks created from a Slack message): the agent drafts the body; the harness
   wraps it in the `[slack]` format (greeting, body, sign-off), shows it to you and sends it
   back to the same conversation or thread only on your yes. Settings in `[slack]`.
8. **Report**: `runs/<id>/report.md` + a macOS notification (and iPhone, if you set up ntfy).
   Every agent call's tokens go to `runs/<id>/usage.jsonl` for `sadyanx usage`.

Statuses: `draft → ready → running → verifying → done`, or `needs-approval`, `blocked`,
`failed`, `killed`. A `blocked`/`failed`/`killed` task can be run again: it resumes the same
agent session with the task's current description (edit it to give the agent what it lacked).
If the Mac shuts down or the runner dies mid-task, the next `list`/`show`/`run` marks it
`killed` ("interrupted") and `run` resumes it. While a task runs the Mac does not idle-sleep.

## Guardrails

| | What | Examples |
|---|---|---|
| ⛔ Forbidden | the agent can never do it | push (the harness does), writing outside the worktree, reading `.env`/keys/keychain, `sudo`, global installs, changing remotes, opening PRs |
| ⏸ Approval | supervised: asked on the spot · autonomous: parked, and you get notified | deploys (`firebase deploy`, `terraform apply`, `make deploy`, `kubectl apply`, `npm publish`...), `gh pr merge`, commenting on PRs/issues/Jira, `curl` writing to an external service, deleting branches, migrations, DB clients, Slack writes (reactions, canvases...) |
| ✅ Autonomous | everything else | editing, committing, running tests/builds, reading Jira, moving a ticket's status, creating private Claude Docs |

- **Slack messages** are never sent by an agent: a reply goes in its result, and the harness
  sends it after your approval in a session allowed to make exactly that one call.
- Approvable actions must run **alone** in their call: what you approve is exactly that
  action, and it is good for one use.
- **Repeated calls**: the 3rd identical call in a row is denied and the agent is told to change
  approach (`max_identical_calls` in `[guard]`).
- **Text written by others** (the ticket as written, web pages) is data, not instructions: `/task`
  keeps the ticket text in a last `## Ticket (verbatim)` section and the runner fences it off.
- **Kill switch**: `sadyanx kill` stops every agent; the guard denies every action and nothing
  new starts until `sadyanx unkill`.
- **Sandbox** (optional, per repo): `sandbox = true` in `repos.toml` runs the agent's commands
  inside Claude Code's OS sandbox: writes only to the worktree, no reads of credentials, network
  only to allowlisted domains. Check it holds on your Mac with `python3 tests/sandbox_probe.py`
  (about a cent). Settings in `[sandbox]` of `sadyanx.toml`.
- **Audit**: every tool call and its verdict go to `runs/<id>/audit.jsonl`; the full
  transcript to `runs/<id>/stream.jsonl`.

## Configuration reference

- `config/sadyanx.toml` (from `sadyanx.example.toml`, which is also used when yours is missing):
  `[defaults]` default mode, run mode, budget and time per task, attempts, `/code-review` level;
  `[modes.*]` which model and effort each mode uses; `[pr_review]` the review bot's comment
  marker and how long to wait for it; `[pr_ci]` how long to wait for CI, fix rounds, the Jira
  status a ready PR moves to; `[slack]` the format of Slack replies; `[notify]`; `[guard]`
  extra forbidden or gated patterns; `[sandbox]`.
- `config/repos.toml` (from `repos.example.toml`): the repos agents may work in. A code task only
  runs on a registered repo.
- Each task can override `mode`, `effort`, `budget_usd` and `timeout_min` in its frontmatter.

## Layout

```
bin/sadyanx            CLI (and the PreToolUse hook: `sadyanx hook guard`)
sadyanxlib/            guard, runner, approvals, git/PRs, reports, notifications
prompts/               agent rules and prompt templates
commands/task.md       the /task slash command (linked into ~/.claude/commands)
config/                *.example.toml (tracked); your sadyanx.toml, repos.toml (local)
tasks/                 the queue: one .md per task (local)
runs/<id>/             report.md, result.json, logs and transcripts (local)
~/.sadyanx/            worktrees, research workspaces, kill switch, pids
```

## Development

```sh
python3 -m unittest discover -s tests   # guard, queue, hook protocol (no network, no cost)
tests/e2e.sh                            # real Haiku agents on a throwaway repo (~5 cents)
python3 tests/sandbox_probe.py          # the OS sandbox holds on this machine (~1 cent)
sadyanx doctor                          # installation and config
```

No dependencies: Python 3.11+ standard library. Design and open items in [`docs/DESIGN.md`](docs/DESIGN.md).
