# sadyanx

A personal, local harness that lets Claude Code agents work on their own: they take a task
(from Jira or planned with you), do it in an isolated worktree, check themselves against
acceptance criteria and the repo's gates, and hand back a branch with a PR and a report with
evidence. Anything irreversible waits for your approval.


## Day to day (from Claude Code)

| You want | Type |
|---|---|
| A task from a ticket | `/task jira PROJ-1234` (optionally `/task jira PROJ-1234 my-backend`) |
| A task from what you just planned | `/task new` |
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
and switch a repo to `autonomous` once its runs bring no surprises. Run `sadyanx install`
once so Claude Code does not ask whether to trust each new folder.

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
   check reads the plan and stops on anything the task does not explain. When it is green, the
   Jira ticket moves to Code Review and you get "PR ready for your review"; the merge is yours.
   Settings in `[pr_ci]` of `sadyanx.toml`.
7. **Report**: `runs/<id>/report.md` + a macOS notification (and iPhone, if you set up ntfy).
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
| ⏸ Approval | supervised: asked on the spot · autonomous: parked, and you get notified | deploys (`firebase deploy`, `terraform apply`, `make deploy`, `kubectl apply`, `npm publish`...), `gh pr merge`, commenting on PRs/issues/Jira, `curl` writing to an external service, deleting branches, migrations, DB clients |
| ✅ Autonomous | everything else | editing, committing, running tests/builds, reading Jira, moving a ticket's status, creating private Claude Docs |

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

## Configuration

Your configuration lives on your machine, next to the code but out of git: `sadyanx install`
creates `config/sadyanx.toml` and `config/repos.toml` from the `*.example.toml` files the repo
ships, and you edit those. The task queue (`tasks/`) and every run (`runs/`) stay local too.

- `config/sadyanx.toml`: default mode and the mode table, budget and time, notifications, extra
  guard rules. On a Max plan the budget is a list-price estimate; it still bounds the tokens.
- `config/repos.toml`: registered repos (path, `personal`/`work`, base, setup, gates, branch
  pattern, how to publish, whether to apply the external PR review and watch the PR's CI). A
  code task only runs on a registered repo. Watching Bitbucket Pipelines needs a token with
  `read:pipeline` and `write:pipeline`; CodeBuild needs a logged-in `aws_profile`.
- HTTPS (Bitbucket PRs, ntfy) uses the system's CA certificates when Python has none of its own
  (the python.org installer); `sadyanx doctor` shows which.
- Each task can override `mode`, `budget_usd` and `timeout_min` in its frontmatter.

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
