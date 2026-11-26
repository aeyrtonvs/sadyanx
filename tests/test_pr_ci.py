"""The PR's CI: watch it with the external review, fix what fails, retry once, check the plan."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sadyanxlib import approvals, ci, config, paths, report, runner, tasks  # noqa: E402
from test_pr_review import GOOD, PR_URL, REVIEW, Clock, comment  # noqa: E402

FIXED = {**GOOD, "summary": "lint: change — unused import removed", "ci_cause": "change"}
TRANSIENT = {**GOOD, "summary": "unit tests: transient — the runner lost its network", "ci_cause": "transient"}
OUTSIDE = {**GOOD, "summary": "npm audit: outside — new CVE in lodash, not touched", "ci_cause": "outside"}
BLAMED = {**GOOD, "summary": "lint: change — could not fix it", "ci_cause": "change"}


def run(state: str, ref: str = "r1", failed=()) -> ci.CiRun:
    return ci.CiRun(state=state, ref=ref, url=f"https://ci/{ref}", commit="",
                    steps=[ci.Step(name=n, state="failed", ref=n) for n in failed])


class FakeCI:
    """A CI reader whose answers per poll are scripted; the last one repeats."""
    name = "Fake CI"

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []
        self.retried = []

    def latest(self, commit, pr_id, also=""):
        self.calls.append((commit, pr_id, also))
        item = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(item, Exception):
            raise item
        return item

    def logs(self, run_, steps):
        for s in steps:
            s.log = f"\x1b[31mlog of {s.name}\x1b[0m: error TS2304"

    def plan_steps(self, run_):
        return [ci.Step(name="Terraform Plan (test)", state="passed", ref="plan")]

    def retry(self, run_):
        self.retried.append(run_.ref)
        return run_.ref + "-rerun"


class CiCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        os.environ["SADYANX_ROOT"] = str(root / "root")
        os.environ["SADYANX_STATE"] = str(root / "state")
        (root / "root" / "config").mkdir(parents=True)
        self.cfg = config.load()
        self.cfg.defaults["keep_awake"] = False
        self.cfg.notify["macos"] = False
        self.repo = config.Repo(name="demo", path=root / "repo", kind="work", base="develop",
                                bitbucket_keychain="kc", pr_ci="pipelines")
        self.cfg.repos["demo"] = self.repo
        self.task = tasks.create("Add percent", "## Acceptance criteria\n- [ ] x", type="code", repo="demo",
                                 status="ready", branch="feature/X-1-percent", session_id="sess-1",
                                 pr_url=PR_URL, jira="X-1")
        self.r = runner.Runner(self.task, self.cfg, run_mode="autonomous")
        self.r.workdir.mkdir(parents=True)
        self.clock = Clock()
        self.heads = iter(["h1", "h2", "h3", "h4", "h5", "h6"])
        self.head = "h1"
        self.patches = [
            mock.patch("builtins.print"),
            mock.patch.object(runner.time, "monotonic", self.clock.monotonic),
            mock.patch.object(runner.time, "sleep", self.clock.sleep),
            mock.patch.object(runner.gitops, "remote_url", return_value="git@bitbucket.org:ws/demo.git"),
            mock.patch.object(runner.gitops, "dirty", return_value=[]),
            mock.patch.object(self.r, "pr_head", side_effect=lambda: self.head),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        os.environ.pop("SADYANX_ROOT", None)
        os.environ.pop("SADYANX_STATE", None)
        self.tmp.cleanup()

    def follow(self, answers, results=(FIXED,), commits=True, comments=None, gates=None, jira="X-1 in CODE REVIEW",
               plan=None):
        """Run pr_review() against a scripted CI. Each push moves the PR's head to a new commit."""
        fake = FakeCI(answers)
        results = list(results)
        m = {"fake": fake}

        def push(*_):
            self.head = next(self.heads) + "-pushed"

        with mock.patch.object(runner.ci, "reader", return_value=fake), \
                mock.patch.object(runner.gitops, "bitbucket_pr_comments", side_effect=comments or [[]]) as m["comments"], \
                mock.patch.object(self.r, "invoke", side_effect=lambda *a: {"structured": results.pop(0) if len(results) > 1 else results[0]}) as m["invoke"], \
                mock.patch.object(self.r, "run_gates", side_effect=gates or (lambda attempt: [])), \
                mock.patch.object(self.r, "code_review") as m["code_review"], \
                mock.patch.object(runner.gitops, "commits_ahead", return_value=["abc fix"] if commits else []), \
                mock.patch.object(runner.gitops, "push", side_effect=push) as m["push"], \
                mock.patch.object(self.r, "move_jira", return_value=jira) as m["jira"], \
                mock.patch.object(self.r, "check_plan", side_effect=plan or (lambda *a: None)) as m["plan"], \
                mock.patch.object(self.r, "notify") as m["notify"]:
            status = self.r.pr_review()
        return status, m

    def reason(self) -> str:
        return tasks.find(self.task.id).get("reason")

    def saved_task(self):
        return tasks.find(self.task.id)


class TestCiPasses(CiCase):
    def test_green_ci_makes_the_pr_ready_and_moves_the_ticket(self):
        status, m = self.follow([None, run("running"), run("passed")])
        self.assertEqual(status, "done")
        self.assertEqual(self.clock.sleeps, 2)
        self.assertEqual(m["fake"].calls[0], ("h1", 42, ""))
        m["invoke"].assert_not_called()
        m["jira"].assert_called_once()
        self.assertEqual(self.reason(), "PR ready for your review: Fake CI passed, X-1 in CODE REVIEW")
        self.assertEqual(self.saved_task().get("phase"), "work")
        rec = json.loads((self.r.run_dir / "ci.json").read_text())
        self.assertEqual(rec["provider"], "Fake CI")
        self.assertEqual([(x["commit"], x["state"], x["outcome"]) for x in rec["runs"]], [("h1", "passed", "passed")])
        text = (self.r.run_dir / "report.md").read_text()
        self.assertIn("## PR CI (Fake CI)", text)
        self.assertIn("[run](https://ci/r1)", text)

    def test_plan_as_expected(self):
        self.repo.plan_check = True
        verdict = {"expected": True, "unexpected": [], "drift": ["tags on the ALB"], "summary": "ok", "changes": []}
        status, m = self.follow([run("passed")], plan=lambda *a: verdict)
        self.assertEqual(status, "done")
        self.assertIn("plan as expected (1 drift note(s))", self.reason())

    def test_unexpected_plan_stops_without_fixing(self):
        self.repo.plan_check = True
        verdict = {"expected": False, "unexpected": ["destroys aws_db_instance.main"], "drift": [], "summary": "",
                   "changes": []}
        status, m = self.follow([run("passed")], plan=lambda *a: verdict)
        self.assertEqual(status, "blocked")
        self.assertIn("the plan is not the expected one: destroys aws_db_instance.main", self.reason())
        m["invoke"].assert_not_called()
        m["jira"].assert_not_called()
        self.assertEqual(self.saved_task().get("phase"), "pr-review")  # a rerun checks again

    def test_environments_the_ci_does_not_plan_are_noted_not_blocking(self):
        self.repo.plan_check, self.repo.plan_envs = True, ["test"]
        verdict = {"expected": True, "unexpected": [], "drift": [], "summary": "ok", "changes": [],
                   "unplanned": [{"environment": "prod", "check": "adds the proxy alarms"}]}
        status, m = self.follow([run("passed")], plan=lambda *a: verdict)
        self.assertEqual(status, "done")
        self.assertIn("not planned by this CI: prod", self.reason())
        m["jira"].assert_called_once()

    def test_a_plan_that_cannot_be_checked_is_not_ready(self):
        self.repo.plan_check = True
        status, m = self.follow([run("passed")], plan=lambda *a: None)
        self.assertEqual(status, "blocked")
        self.assertIn("could not be checked", self.reason())
        m["jira"].assert_not_called()

    def test_without_a_ticket_or_a_status_jira_is_left_alone(self):
        self.task.set(jira="")
        self.task.save()
        r = runner.Runner(self.task, self.cfg, run_mode="autonomous")
        with mock.patch.object(runner.subprocess, "run") as sp:
            self.assertEqual(r.move_jira(), "")
            self.cfg.pr_ci["ready_jira_status"] = ""
            self.task.set(jira="X-1")
            self.assertEqual(r.move_jira(), "")
        sp.assert_not_called()


class TestCiFixRounds(CiCase):
    def test_a_failure_goes_to_the_agent_and_the_fix_is_pushed_and_watched(self):
        status, m = self.follow([run("failed", "r1", failed=["Validate: Lint"]), run("passed", "r2")])
        self.assertEqual(status, "done")
        prompt, resume, mode, phase, attempt = m["invoke"].call_args.args
        self.assertTrue(resume)
        self.assertEqual(phase, "ci-fix")
        self.assertIn("fix round 1 of 3", prompt)
        self.assertRegex(prompt, r"<untrusted-[0-9a-f]{8} source=\"CI log\">")
        self.assertIn("log of Validate: Lint: error TS2304", prompt)
        self.assertNotIn("\x1b[31m", prompt)
        self.assertIn("Do not push", prompt)
        m["code_review"].assert_not_called()
        m["push"].assert_called_once()
        self.assertEqual(m["fake"].calls[-1][0], "h1-pushed")  # the new commit's CI
        task = self.saved_task()
        self.assertEqual(task.num("ci_rounds"), 1)
        self.assertIn("lint: change", task.get("ci_diagnosis"))
        self.assertTrue(list(self.r.run_dir.glob("ci-1-failed/01-Validate_Lint.log")))
        self.assertEqual(self.reason(), "PR ready for your review: Fake CI passed, X-1 in CODE REVIEW")
        self.assertFalse((self.r.run_dir / "pr-review.json").exists())  # no review on this repo

    def test_stops_after_max_rounds(self):
        status, m = self.follow([run("failed", failed=["tests"])])
        self.assertEqual(status, "blocked")
        self.assertEqual(m["invoke"].call_count, 3)
        self.assertEqual(m["push"].call_count, 3)
        self.assertIn("still fails after 3 fix round(s)", self.reason())
        self.assertIn("the agent's last diagnosis: lint: change", self.reason())
        self.assertEqual(self.saved_task().num("rework"), 0)

    def test_fixes_that_fail_the_gates_are_not_pushed(self):
        status, m = self.follow([run("failed", failed=["tests"])], gates=lambda a: ["Gate `make test` failed"])
        self.assertEqual(status, "failed")
        m["push"].assert_not_called()
        self.assertIn("nothing pushed, the PR is as it was", self.reason())
        self.assertEqual(self.saved_task().get("phase"), "pr-review")

    def test_transient_is_retried_once_then_handed_over(self):
        status, m = self.follow([run("failed", "r1", failed=["tests"]),
                                 run("failed", "r1", failed=["tests"]),  # the rerun is not listed yet
                                 run("failed", "r1-rerun", failed=["tests"])],
                                results=(TRANSIENT,), commits=False)
        self.assertEqual(status, "blocked")
        self.assertEqual(m["fake"].retried, ["r1"])
        self.assertEqual(m["invoke"].call_count, 1)
        self.assertEqual(m["fake"].calls[-1], ("h1", 42, "r1-rerun"))
        self.assertIn("failed again after a retry", self.reason())
        self.assertIn("the runner lost its network", self.reason())
        task = self.saved_task()
        self.assertEqual((task.get("ci_retried"), task.get("ci_retried_ref"), task.num("rework")), ("h1", "r1", 0))

    def test_a_rerun_that_passes_makes_the_pr_ready(self):
        status, m = self.follow([run("failed", "r1", failed=["tests"]), run("passed", "r1-rerun")],
                                results=(TRANSIENT,), commits=False)
        self.assertEqual(status, "done")
        self.assertEqual(m["fake"].retried, ["r1"])
        outcomes = [x["outcome"] for x in json.loads((self.r.run_dir / "ci.json").read_text())["runs"]]
        self.assertEqual(outcomes, ["failed: fix round", "retried (transient failure)", "passed"])

    def test_outside_the_change_goes_to_you_without_a_retry(self):
        status, m = self.follow([run("failed", failed=["Validate: npm audit"])], results=(OUTSIDE,), commits=False)
        self.assertEqual(status, "blocked")
        self.assertEqual(m["fake"].retried, [])
        self.assertIn("outside the change", self.reason())
        self.assertIn("new CVE in lodash", self.reason())

    def test_blamed_on_the_change_but_nothing_committed(self):
        status, m = self.follow([run("failed", failed=["lint"])], results=(BLAMED,), commits=False)
        self.assertEqual(status, "blocked")
        self.assertIn("committed no fix", self.reason())


class TestWaiting(CiCase):
    def test_no_run_for_the_commit_in_time(self):
        status, m = self.follow([None])
        self.assertEqual(status, "blocked")
        self.assertEqual(len(m["fake"].calls), 11)  # right away, then every minute up to start_min
        self.assertIn("no Fake CI run for h1 started within 10 min", self.reason())
        self.assertIn("waits again", self.reason())
        self.assertEqual(self.saved_task().get("phase"), "pr-review")

    def test_a_run_that_never_finishes(self):
        status, m = self.follow([run("running")])
        self.assertEqual(status, "blocked")
        self.assertEqual(len(m["fake"].calls), 31)
        self.assertIn("did not finish within 30 min (https://ci/r1)", self.reason())

    def test_an_expired_aws_session_tells_you_once_and_keeps_polling(self):
        expired = ci.CiError("the AWS session expired", login="aws sso login --profile p")
        status, m = self.follow([expired, expired, run("passed")])
        self.assertEqual(status, "done")
        login = [c for c in m["notify"].call_args_list if "aws sso login --profile p" in c.args[1]]
        self.assertEqual(len(login), 1)

    def test_an_aws_session_still_expired_at_the_end(self):
        status, m = self.follow([ci.CiError("expired", login="aws sso login --profile p")])
        self.assertEqual(status, "blocked")
        self.assertIn("run `aws sso login --profile p`", self.reason())

    def test_kill_switch(self):
        paths.pids_dir().mkdir(parents=True, exist_ok=True)
        (paths.pids_dir() / f"{self.task.id}.kill").write_text("now")
        status, m = self.follow([run("passed")])
        self.assertEqual(status, "killed")
        self.assertEqual(m["fake"].calls, [])


class TestWithTheReview(CiCase):
    def setUp(self):
        super().setUp()
        self.repo.pr_review = True

    def test_review_and_failed_ci_go_in_one_round(self):
        status, m = self.follow([run("failed", "r1", failed=["lint"]), run("passed", "r2")],
                                comments=[[], [comment(REVIEW)]])
        self.assertEqual(status, "done")
        self.assertEqual(m["invoke"].call_count, 1)
        prompt, _, _, phase, _ = m["invoke"].call_args.args
        self.assertEqual(phase, "pr-review")
        self.assertIn("## The external review", prompt)
        self.assertIn("percent() divides by zero", prompt)
        self.assertIn("## The PR's CI failed", prompt)
        m["push"].assert_called_once()
        saved = json.loads((self.r.run_dir / "pr-review.json").read_text())
        self.assertTrue(saved["handled"])
        self.assertEqual(m["comments"].call_count, 2)  # not polled again after the round
        self.assertIn("external PR review: 1 commit(s) pushed to the PR", self.reason())
        self.assertEqual(self.saved_task().num("ci_rounds"), 1)

    def test_review_findings_with_green_ci_then_the_plan_of_the_new_commit(self):
        self.repo.plan_check = True
        verdict = {"expected": True, "unexpected": [], "drift": [], "summary": "ok", "changes": []}
        status, m = self.follow([run("passed", "r1"), run("passed", "r2")], comments=[[comment(REVIEW)]],
                                plan=lambda *a: verdict)
        self.assertEqual(status, "done")
        self.assertEqual(m["invoke"].call_args.args[3], "pr-review")
        m["plan"].assert_called_once()  # only on the commit with the review's fixes
        self.assertEqual(m["fake"].calls[-1][0], "h1-pushed")
        self.assertEqual(self.saved_task().num("ci_rounds"), 0)  # a review round is not a CI round

    def test_no_review_in_time_and_green_ci(self):
        status, m = self.follow([run("running"), run("passed")], comments=[[]] * 20)
        self.assertEqual(status, "done")
        m["invoke"].assert_not_called()
        self.assertIn("no external PR review within 10 min", self.reason())
        self.assertEqual(m["comments"].call_count, 11)

    def test_review_is_applied_even_if_the_ci_is_late(self):
        status, m = self.follow([None, None, None, None, None, None, None, None, None, None, None,
                                 run("passed", "r2")], comments=[[comment(REVIEW)]])
        self.assertEqual(status, "done")
        self.assertEqual(m["invoke"].call_count, 1)
        self.assertEqual(m["invoke"].call_args.args[3], "pr-review")


class TestGettingThere(CiCase):
    def test_follows_prs_for_ci_alone(self):
        self.assertTrue(self.r.follows_pr(PR_URL))
        self.assertFalse(self.r.wants_pr_review(PR_URL))
        self.assertFalse(self.r.follows_pr("https://github.com/o/r/pull/3"))
        self.repo.pr_ci = ""
        self.assertFalse(self.r.follows_pr(PR_URL))

    def test_approving_the_pr_says_what_it_waits_for(self):
        item = approvals.request_harness(self.r.run_dir, "open-pr", {"host": "bitbucket"}, "third-party", "PR")
        approvals.decide(self.r.run_dir, [item["id"]], "approved")
        with mock.patch.object(runner, "open_pr", return_value=PR_URL):
            self.assertIsNone(runner.apply_harness_approvals(self.task, self.cfg))
        task = tasks.find(self.task.id)
        self.assertEqual((task.status, task.get("phase")), ("ready", "pr-review"))
        self.assertEqual(task.get("reason"), "PR open; waiting for the CI")
        self.repo.pr_review = True
        self.assertEqual(runner.Runner(task, self.cfg).awaited(), "the external review and the CI")

    def test_ci_cause_in_a_supervised_result(self):
        self.assertIsNone(runner.check_result(FIXED))
        self.assertIn("ci_cause", runner.check_result({**GOOD, "ci_cause": "flaky"}))


class TestPlanCheck(CiCase):
    PLAN_LOG = ("Refreshing state...\n\x1b[1mTerraform will perform the following actions:\x1b[0m\n"
                "  # module.x.datadog_monitor.m will be created\nPlan: 1 to add, 0 to change, 0 to destroy.\n")

    def check(self, stdout: str):
        fake = FakeCI([run("passed")])
        fake.logs = lambda r_, steps: [setattr(s, "log", self.PLAN_LOG) for s in steps]
        with mock.patch.object(runner.gitops, "git", side_effect=lambda *a, **k: "diff --git a/main.tf b/main.tf"), \
                mock.patch.object(runner.gitops, "base_ref", return_value="origin/develop"), \
                mock.patch.object(runner.subprocess, "run",
                                  return_value=subprocess.CompletedProcess([], 0, stdout, "")) as sp:
            verdict = self.r.check_plan(fake, run("passed"))
        return verdict, sp

    def test_the_checker_sees_task_diff_and_plan_fenced(self):
        out = {"total_cost_usd": 0.02, "structured_output": {
            "expected": True, "summary": "adds the monitor", "unexpected": [], "drift": [],
            "changes": [{"address": "module.x.datadog_monitor.m", "action": "create", "why": "main.tf"}]}}
        verdict, sp = self.check(json.dumps(out))
        self.assertTrue(verdict["expected"])
        args = sp.call_args.args[0]
        prompt = args[args.index("-p") + 1]
        self.assertIn("# Add percent", prompt)
        self.assertRegex(prompt, r"source=\"CI log\">[\s\S]*Plan: 1 to add")
        self.assertRegex(prompt, r"source=\"diff\">")
        self.assertNotIn("\x1b[1m", prompt)
        self.assertEqual(args[args.index("--tools") + 1], "")  # no tools
        saved = json.loads((self.r.run_dir / "plan-check.json").read_text())
        self.assertEqual(saved["changes"][0]["action"], "create")
        self.assertIn("Plan: 1 to add", (self.r.run_dir / "plan.txt").read_text())
        rec = json.loads((self.r.run_dir / "ci.json").read_text())
        self.assertTrue(rec["plan"]["expected"])
        report.write(self.task, self.r.run_dir, None)
        text = (self.r.run_dir / "report.md").read_text()
        self.assertIn("### Plan check: as expected", text)
        self.assertIn("| `module.x.datadog_monitor.m` | create | main.tf |", text)

    def test_the_checker_is_told_which_environments_the_ci_plans(self):
        out = {"structured_output": {"expected": True, "summary": "", "unexpected": [], "drift": [],
                                     "changes": [], "unplanned": []}}
        _, sp = self.check(json.dumps(out))
        args = sp.call_args.args[0]
        self.assertIn("are not declared", args[args.index("-p") + 1])
        self.repo.plan_envs = ["test"]
        _, sp = self.check(json.dumps(out))
        args = sp.call_args.args[0]
        self.assertIn("This CI plans only these environments: `test`", args[args.index("-p") + 1])
        self.assertIn('"unplanned"', args[args.index("--json-schema") + 1])

    def test_unplanned_is_kept_only_for_environments_declared_unplanned(self):
        def verdict_for(*unplanned):
            out = {"structured_output": {"expected": True, "summary": "", "unexpected": [], "drift": [],
                                         "changes": [], "unplanned": list(unplanned)}}
            return self.check(json.dumps(out))[0]

        prod = {"environment": "prod", "check": "the new alarms"}
        # Nothing declared: a missing plan is never waved through.
        verdict = verdict_for(prod)
        self.assertFalse(verdict["expected"])
        self.assertEqual(verdict["unplanned"], [])
        self.assertIn("no plan for `prod`", verdict["unexpected"][0])
        self.repo.plan_envs = ["test"]
        verdict = verdict_for(prod)
        self.assertTrue(verdict["expected"])
        self.assertEqual(verdict["unplanned"], [prod])
        saved = json.loads((self.r.run_dir / "plan-check.json").read_text())
        self.assertEqual(saved["unplanned"], [prod])
        report.write(self.task, self.r.run_dir, None)
        self.assertIn("- `prod`: the new alarms", (self.r.run_dir / "report.md").read_text())
        # An environment the CI does plan, an empty name or a malformed entry: unexpected.
        for bad in ({"environment": "test", "check": "x"}, {"environment": " ", "check": "x"}, "prod"):
            with self.subTest(bad=bad):
                verdict = verdict_for(prod, bad)
                self.assertFalse(verdict["expected"])
                self.assertEqual(verdict["unplanned"], [prod])
                self.assertEqual(len(verdict["unexpected"]), 1)

    def test_a_broken_checker_gives_no_verdict(self):
        verdict, _ = self.check("not json")
        self.assertIsNone(verdict)
        verdict, _ = self.check(json.dumps({"structured_output": {"summary": "?"}}))
        self.assertIsNone(verdict)


class TestJira(CiCase):
    def test_moves_the_ticket_in_a_guarded_session_with_only_mcp(self):
        out = {"total_cost_usd": 0.004, "structured_output": {"moved": True, "status": "CODE REVIEW", "note": "ok"}}
        with mock.patch.object(runner.subprocess, "run",
                               return_value=subprocess.CompletedProcess([], 0, json.dumps(out), "")) as sp:
            self.assertEqual(self.r.move_jira(), "X-1 in CODE REVIEW")
        args, kwargs = sp.call_args.args[0], sp.call_args.kwargs
        prompt = args[args.index("-p") + 1]
        self.assertIn('Move the Jira issue X-1 to the status "CODE REVIEW"', prompt)
        self.assertEqual(args[args.index("--tools") + 1], "ToolSearch")
        self.assertNotIn("--strict-mcp-config", args)  # the MCP servers are what it needs
        self.assertIn("--settings", args)  # the guard hook
        self.assertTrue(kwargs["env"]["SADYANX_GUARD_CTX"].endswith("guard-review.json"))  # nothing approvable
        self.assertEqual(args[args.index("--model") + 1], self.cfg.mode("cheap").model)

    def test_a_failed_move_does_not_hold_the_pr(self):
        with mock.patch.object(runner.subprocess, "run", side_effect=subprocess.TimeoutExpired("claude", 300)):
            self.assertEqual(self.r.move_jira(), "X-1 not moved (the Jira session failed)")
        out = {"structured_output": {"moved": False, "status": "IN PROGRESS", "note": "no transition to CODE REVIEW"}}
        with mock.patch.object(runner.subprocess, "run",
                               return_value=subprocess.CompletedProcess([], 0, json.dumps(out), "")):
            self.assertEqual(self.r.move_jira(), "X-1 not moved: no transition to CODE REVIEW")


class TestReaders(unittest.TestCase):
    def test_plan_excerpt(self):
        self.assertEqual(ci.plan_excerpt("make lint\nok\n"), "")
        short = "init\nTerraform will perform the following actions:\n  # a will be created\nPlan: 1 to add, 0 to change, 0 to destroy.\n"
        self.assertEqual(ci.plan_excerpt(short), short.strip())
        noise = "x" * 200 + "\n"
        long = noise * 50 + short + noise * 50 + "Note: Objects have changed outside of Terraform\n  ~ drift\n" \
            "No changes. Your infrastructure matches the configuration.\n" + noise * 50
        got = ci.plan_excerpt(long, limit=2000)
        self.assertIn("# a will be created", got)
        self.assertIn("Plan: 1 to add", got)
        self.assertIn("~ drift", got)
        self.assertNotIn("x" * 200, got)

    def test_tail_keeps_the_end(self):
        text = "\n".join(f"line {i}" for i in range(10000))
        got = ci.tail(text, limit=200)
        self.assertTrue(got.startswith("[... "))
        self.assertTrue(got.endswith("line 9999"))
        self.assertEqual(ci.tail("\x1b[32mok\x1b[0m"), "ok")

    def bitbucket(self, pages):
        bb = ci.BitbucketPipelines("ws", "demo", "kc")
        calls = []

        def request(url, data=None, raw=False):
            calls.append((url, data))
            if "/steps/" in url:
                return {"values": [
                    {"uuid": "s1", "name": "Validate: Lint", "state": {"name": "COMPLETED", "result": {"name": "FAILED"}}},
                    {"uuid": "s2", "name": "Typecheck", "state": {"name": "COMPLETED", "result": {"name": "STOPPED"}}},
                    {"uuid": "s3", "name": "Integration", "state": {"name": "COMPLETED", "result": {"name": "NOT_RUN"}}}]}
            if data is not None:
                return {"uuid": "{new}"}
            return pages.pop(0)
        bb._request = request
        return bb, calls

    def test_bitbucket_newest_pr_pipeline_for_the_commit(self):
        target = {"type": "pipeline_pullrequest_target", "commit": {"hash": "abc"}, "pullrequest": {"id": 42},
                  "source": "feature/x", "destination": "develop", "selector": {"type": "pull-requests", "pattern": "**"}}
        pages = [{"values": [
            {"uuid": "{other}", "build_number": 9, "target": {**target, "commit": {"hash": "zzz"}},
             "state": {"name": "IN_PROGRESS"}},
            {"uuid": "{branch}", "build_number": 8, "target": {"type": "pipeline_ref_target", "commit": {"hash": "abc"}},
             "state": {"name": "COMPLETED", "result": {"name": "SUCCESSFUL"}}},
            {"uuid": "{pr}", "build_number": 7, "target": target, "created_on": "t",
             "state": {"name": "COMPLETED", "result": {"name": "FAILED"}}}]}]
        bb, calls = self.bitbucket(pages)
        found = bb.latest("abc", 42)
        self.assertEqual((found.ref, found.state), ("{pr}", "failed"))
        self.assertEqual(found.url, "https://bitbucket.org/ws/demo/pipelines/results/7")
        self.assertEqual([(s.name, s.state) for s in found.steps],
                         [("Validate: Lint", "failed"), ("Typecheck", "stopped"), ("Integration", "skipped")])
        self.assertEqual([s.name for s in found.failed_steps], ["Validate: Lint"])
        self.assertIn("sort=-created_on", calls[0][0])
        self.assertEqual(bb.retry(found), "{new}")
        url, body = calls[-1]
        self.assertTrue(url.endswith("/pipelines/"))
        self.assertEqual(body["target"]["commit"], {"hash": "abc"})
        self.assertEqual(body["target"]["pullrequest"], {"id": 42})

    def test_bitbucket_running_and_absent(self):
        running = {"uuid": "{p}", "target": {"type": "pipeline_pullrequest_target", "commit": {"hash": "abc"}},
                   "state": {"name": "PENDING"}}
        bb, _ = self.bitbucket([{"values": [running]}, {"values": []}])
        self.assertEqual(bb.latest("abc", 42).state, "running")
        self.assertIsNone(bb.latest("abc", 42))

    def test_bitbucket_403_says_which_scope(self):
        bb = ci.BitbucketPipelines("ws", "demo", "kc")
        err = ci.urllib.error.HTTPError("u", 403, "Forbidden", {}, None)
        err.read = lambda: b"forbidden"
        with mock.patch.object(ci.gitops, "_bitbucket_auth", return_value="Basic x"), \
                mock.patch.object(ci.net, "urlopen", side_effect=err):
            with self.assertRaisesRegex(ci.CiError, "read:pipeline"):
                bb.latest("abc", 42)

    def test_codebuild_pr_builds_by_initiator_and_commit(self):
        cb = ci.CodeBuild("proj-develop", "prof", initiator="codepipeline/pr-develop")
        builds = [
            {"id": "proj:merge", "resolvedSourceVersion": "abc1234", "initiator": "codepipeline/build-develop",
             "buildStatus": "SUCCEEDED", "startTime": "2026-11-18T10:05"},
            {"id": "proj:old", "resolvedSourceVersion": "abc1234", "initiator": "codepipeline/pr-develop",
             "buildStatus": "FAILED", "startTime": "2026-11-18T10:00", "arn": "arn:aws:codebuild:us-east-1:1:build/x",
             "phases": [{"phaseType": "BUILD", "phaseStatus": "FAILED"}, {"phaseType": "INSTALL", "phaseStatus": "SUCCEEDED"}]},
            {"id": "proj:retry", "resolvedSourceVersion": "abc1234", "initiator": "someone",
             "buildStatus": "IN_PROGRESS", "startTime": "2026-11-18T10:10"},
            {"id": "proj:otherpr", "resolvedSourceVersion": "zzz1234", "initiator": "codepipeline/pr-develop",
             "buildStatus": "SUCCEEDED", "startTime": "2026-11-18T10:20"}]
        answers = {"list-builds-for-project": {"ids": [b["id"] for b in builds]},
                   "batch-get-builds": {"builds": builds}}
        with mock.patch.object(cb, "aws", side_effect=lambda *a, **k: answers[a[1]]) as aws:
            got = cb.latest("abc1234", 42)
            self.assertEqual((got.ref, got.state), ("proj:old", "failed"))
            self.assertIn("region=us-east-1", got.url)
            self.assertEqual([s.name for s in got.failed_steps], ["BUILD"])
            self.assertIn("--no-paginate", aws.call_args_list[0].args)
            self.assertEqual(cb.latest("abc1234", 42, also="proj:retry").ref, "proj:retry")  # the rerun, by its id
        self.assertEqual(ci.failed_steps(cb, got)[0].name, "CodeBuild proj-develop (failed phase: BUILD)")
        cb2 = ci.CodeBuild("proj", "prof")
        self.assertTrue(cb2._is_pr({"sourceVersion": "pr/42"}, 42, ""))
        self.assertFalse(cb2._is_pr({"sourceVersion": "refs/heads/develop"}, 42, ""))

    def test_codebuild_matches_abbreviated_resolved_sha(self):
        cb = ci.CodeBuild("proj", "prof", initiator="codepipeline/pr-develop")
        full = "af5e4e503ca12c6aa9862209193a89ca122fcb8b"
        builds = [{"id": "proj:1", "resolvedSourceVersion": full[:12], "initiator": "codepipeline/pr-develop",
                   "buildStatus": "SUCCEEDED", "startTime": "2026-11-18T10:00"},
                  {"id": "proj:2", "resolvedSourceVersion": "ae01ce09abcd", "initiator": "codepipeline/pr-develop",
                   "buildStatus": "SUCCEEDED", "startTime": "2026-11-18T10:05"}]
        answers = {"list-builds-for-project": {"ids": ["proj:1", "proj:2"]}, "batch-get-builds": {"builds": builds}}
        with mock.patch.object(cb, "aws", side_effect=lambda *a, **k: answers[a[1]]):
            self.assertEqual(cb.latest(full, 42).ref, "proj:1")
            self.assertIsNone(cb.latest("0" * 40, 42))
        self.assertFalse(cb._same_commit("", full))
        self.assertFalse(cb._same_commit("abc", "abcdef0"))  # too short to trust

    def test_codebuild_expired_sso_asks_you_to_log_in(self):
        cb = ci.CodeBuild("proj", "ci-reader")
        expired = subprocess.CompletedProcess([], 255, "", "Error when retrieving token from sso: Token has expired "
                                                           "and refresh failed")
        with mock.patch.object(ci.subprocess, "run", return_value=expired):
            with self.assertRaises(ci.CiError) as ctx:
                cb.latest("abc", 1)
        self.assertEqual(ctx.exception.login, "aws sso login --profile ci-reader")
        denied = subprocess.CompletedProcess([], 254, "", "An error occurred (AccessDeniedException) ...")
        with mock.patch.object(ci.subprocess, "run", return_value=denied):
            with self.assertRaises(ci.CiError) as ctx:
                cb.retry(ci.CiRun("failed", "proj:1", "", "abc"))
        self.assertEqual(ctx.exception.login, "")

    def test_codebuild_log_pages(self):
        cb = ci.CodeBuild("proj", "prof")
        pages = [{"events": [{"message": "a\n"}, {"message": "b\n"}], "nextForwardToken": "t1"},
                 {"events": [{"message": "c\n"}], "nextForwardToken": "t2"},
                 {"events": [], "nextForwardToken": "t2"}]
        r = ci.CiRun("failed", "proj:1", "", "abc", raw={"logs": {"groupName": "g", "streamName": "s"}})
        with mock.patch.object(cb, "aws", side_effect=lambda *a, **k: pages.pop(0)):
            self.assertEqual(cb.log(r), "a\nb\nc")
        self.assertIn("no CloudWatch log", cb.log(ci.CiRun("failed", "x", "", "abc")))


class TestConfig(unittest.TestCase):
    def load(self, harness: str = "", repos: str = "") -> config.Config:
        with tempfile.TemporaryDirectory() as d:
            old = os.environ.get("SADYANX_ROOT")
            os.environ["SADYANX_ROOT"] = d
            try:
                (Path(d) / "config").mkdir()
                (Path(d) / "config" / "sadyanx.toml").write_text(harness)
                (Path(d) / "config" / "repos.toml").write_text(repos)
                return config.load()
            finally:
                if old is None:
                    os.environ.pop("SADYANX_ROOT", None)
                else:
                    os.environ["SADYANX_ROOT"] = old

    BASE = '[repos.a]\npath = "/x"\nbase = "develop"\n'

    def test_defaults_and_a_codebuild_repo(self):
        cfg = self.load(repos=self.BASE)
        self.assertEqual(cfg.pr_ci, {"wait_min": 30, "start_min": 10, "max_rounds": 3,
                                     "ready_jira_status": "CODE REVIEW"})
        self.assertEqual((cfg.repos["a"].pr_ci, cfg.repos["a"].plan_check), ("", False))
        cfg = self.load('[pr_ci]\nmax_rounds = 5\nready_jira_status = ""\n', self.BASE + (
            'pr_ci = "codebuild"\ncodebuild_project = "p-{base}"\ncodebuild_initiator = "codepipeline/pr-{base}"\n'
            'aws_profile = "prof"\nplan_check = true\nplan_envs = ["test"]\n'))
        a = cfg.repos["a"]
        self.assertEqual(a.plan_envs, ["test"])
        self.assertEqual((cfg.pr_ci["max_rounds"], cfg.pr_ci["ready_jira_status"]), (5, ""))
        reader = ci.reader(a, "git@bitbucket.org:ws/a.git", "develop")
        self.assertEqual((reader.project, reader.initiator, reader.profile), ("p-develop", "codepipeline/pr-develop", "prof"))
        cfg = self.load(repos=self.BASE + 'pr_ci = "pipelines"\nbitbucket_keychain = "kc"\n')
        reader = ci.reader(cfg.repos["a"], "git@bitbucket.org:ws/a.git", "develop")
        self.assertEqual((reader.workspace, reader.slug), ("ws", "a"))

    def test_invalid(self):
        for harness, repos in (
                ("", self.BASE + 'pr_ci = "jenkins"\n'),
                ("", self.BASE + 'pr_ci = "pipelines"\n'),                                  # no keychain
                ("", self.BASE + 'pr_ci = "codebuild"\naws_profile = "p"\n'),               # no project
                ("", self.BASE + 'pr_ci = "pipelines"\nbitbucket_keychain = "k"\npublish = "push"\n'),
                ("", self.BASE + 'plan_check = true\n'),                                    # no CI to read
                ("", self.BASE + 'plan_check = "yes"\n'),
                ("", self.BASE + 'plan_envs = ["test"]\n'),                                 # no plan check
                ("", self.BASE + 'pr_ci = "pipelines"\nbitbucket_keychain = "k"\nplan_check = true\n'
                                 'plan_envs = "test"\n'),
                ("", self.BASE + 'pr_ci = "pipelines"\nbitbucket_keychain = "k"\nplan_check = true\n'
                                 'plan_envs = [""]\n'),
                ('[pr_ci]\nmax_rounds = 0\n', ""),
                ('[pr_ci]\nmax_rounds = 2.5\n', ""),
                ('[pr_ci]\nstart_min = 40\n', ""),                                         # beyond wait_min
                ('[pr_ci]\nwait = 5\n', ""),
                ('[pr_ci]\nready_jira_status = false\n', "")):
            with self.subTest(harness=harness, repos=repos), self.assertRaises(config.ConfigError):
                self.load(harness, repos)


if __name__ == "__main__":
    unittest.main()
