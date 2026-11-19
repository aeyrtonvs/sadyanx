The repo's gates pass. An automatic code review (`/code-review`) then read your branch and
reported $count finding(s). It is a second opinion from another tool and it can be wrong. It
runs once per task: nobody re-reviews your fixes, so your judgment here is final, and the
external review on the PR is the next look.

$findings

Go through every finding:

1. Check it against the code on `$branch`: is it real, and does it belong to this task's change?
2. Fix the ones that hold up, adding or adjusting a test when it fits, and commit them on
   `$branch`. Keep the fixes small: do not widen the task.
3. Leave the ones that are wrong, a matter of taste, or outside the task's scope.

Do not push. Then return the structured result again: the acceptance criteria with evidence
as before, and in `summary` one line per finding, by its number, e.g. `#1 applied: <what
changed>` or `#2 not applied: <why>`. The harness runs the gates again; your user reads these
lines in the report.
