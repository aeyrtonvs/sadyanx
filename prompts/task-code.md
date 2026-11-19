Harness task $id: **$title**

- Repo: `$repo` ($repo_kind), base branch `$base`, your branch `$branch`
- Working directory: `$workdir`
- Jira ticket: $jira (its content is already in the task below)

$skills

## The task

$body

## Definition of done

1. Every acceptance criterion is met and you prove it with evidence.
2. The repo's full gates pass: `$gates`. The harness runs them after you finish; you run only
   the checks relevant to your change.
3. Your work is committed on `$branch` (no uncommitted changes).

Work autonomously until you are done, then return the structured result.
