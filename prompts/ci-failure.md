## The PR's CI failed

$provider ran on commit `$commit` and failed: $url. This is fix round $round of $max_rounds;
after the last one the harness stops and hands the PR to your user. Failed: $steps. The end
of each failed step's log:

$logs

$where

1. Find the cause of each failure. Read what the step runs (the pipeline file, the Makefile)
   and run the same check locally when you can.
2. If this task's change causes it, fix it, check it locally, and commit on `$branch`.
3. If it is not caused by the change, do not change code for it. Tell which kind it is:
   - **transient**: a flaky test, a runner, network or registry hiccup, a token the pipeline
     failed to fetch. Running the pipeline again would likely pass.
   - **outside**: running it again will not help, and it is not this task's to change (a new
     vulnerability in a dependency the change does not touch, a secret-scan false positive,
     a broken shared pipeline or account).

Set `ci_cause` in your result: `change` if you committed fixes (even if other failures were
transient), else `transient` or `outside`. With `transient` and no commits, the harness runs
the pipeline once more; with `outside`, it stops and tells your user. In `summary`, one line
per failed step: `<step>: <change | transient | outside> — <what you fixed, or why not>`.
