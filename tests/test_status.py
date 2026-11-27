import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import contextlib  # noqa: E402
import io  # noqa: E402

from sadyanxlib import approvals, cli, paths, status, tasks  # noqa: E402


class TestStatus(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.state = tempfile.TemporaryDirectory()
        os.environ["SADYANX_ROOT"] = self.root.name
        os.environ["SADYANX_STATE"] = self.state.name
        self.done = tasks.create("Finished", "x", type="code", repo="r1", status="done", mode="smart")
        self.parked = tasks.create("Wants to deploy", "x", type="code", repo="r1", status="needs-approval",
                                   mode="balanced")
        approvals.request_tool(paths.run_dir(self.parked.id), "Bash", {"command": "make deploy"}, "deploy",
                               "deploys need your approval")
        now = tasks.now()
        audit = [
            {"ts": now, "tool": "Bash", "verdict": "allow", "category": "", "reason": ""},
            {"ts": now, "tool": "Bash", "verdict": "deny", "category": "secrets", "reason": "secret files: ~/.aws"},
            {"ts": now, "tool": "Write", "verdict": "deny", "category": "write", "reason": "outside the worktree"},
            {"ts": now, "tool": "Bash", "verdict": "ask", "category": "deploy", "reason": "deploy"},
            {"ts": "2000-01-01T00:00:00", "tool": "Bash", "verdict": "deny", "category": "old", "reason": "old"},
        ]
        f = paths.run_dir(self.parked.id) / "audit.jsonl"
        f.write_text("\n".join(json.dumps(r) for r in audit) + "\nnot json\n")

    def tearDown(self):
        os.environ.pop("SADYANX_ROOT", None)
        os.environ.pop("SADYANX_STATE", None)
        self.root.cleanup()
        self.state.cleanup()

    def test_snapshot(self):
        s = status.snapshot(30)
        self.assertFalse(s["kill_switch"])
        self.assertEqual(s["counts"], {"done": 1, "needs-approval": 1})
        self.assertEqual(s["pending_approvals"], 1)
        parked = next(r for r in s["tasks"] if r["id"] == self.parked.id)
        self.assertEqual((parked["mode"], parked["pending"][0]["category"]), ("balanced", "deploy"))
        self.assertIn("make deploy", parked["pending"][0]["text"])
        done = next(r for r in s["tasks"] if r["id"] == self.done.id)
        self.assertEqual(done["pending"], [])
        self.assertIn("by_day", s["usage"])

    def test_guard_summary_counts_the_window_only(self):
        g = status.guard_summary(30)
        self.assertEqual(g["verdicts"], {"allow": 1, "deny": 2, "ask": 1})
        self.assertEqual(g["by_category"], {"deny": {"secrets": 1, "write": 1}, "ask": {"deploy": 1}})
        self.assertEqual({d["category"] for d in g["recent_denials"]}, {"secrets", "write"})
        self.assertEqual(g["recent_denials"][0]["task"], self.parked.id)

    def test_model_free_work_is_counted_not_estimated(self):
        now = tasks.now()
        lines = [
            f"[{now}] gate: make lint", f"[{now}] gate: make test", f"[{now}] setup: make setup",
            f"[{now}] verification failed: 2 problem(s)", f"[{now}] verification passed",
            f"[{now}] pushing feature/x (3 commits)", f"[{now}] PR opened: https://example.com/pr/1",
            f"[{now}] pushing 1 commit(s) to the PR (feature/x)",
            f"[{now}] CI: AWS CodeBuild passed on 50a9a56e (https://example.com/run)",
            f"[{now}] CI: Bitbucket Pipelines failed on c09628a3: lint",
            f"[{now}] CI: rerunning AWS CodeBuild on c09628a3 once (the agent judged it transient)",
            # not counted: they are not a CI result, or a model did the work
            f"[{now}] CI: could not read AWS CodeBuild (expired)",
            f"[{now}] CI: no AWS CodeBuild run for af5e4e50 started within 10 min; applying the review first",
            f"[{now}] plan check: as expected", f"[{now}] review passed", f"[{now}] jira: moved",
            f"[{now}] code review: 2 finding(s) ($0.40)",
            "[2000-01-01T00:00:00] gate: old", "garbage line", "",
        ]
        (paths.run_dir(self.done.id)).mkdir(parents=True, exist_ok=True)
        (paths.run_dir(self.done.id) / "events.log").write_text("\n".join(lines) + "\n")
        self.assertEqual(status.model_free(30), {"gates": 2, "setup": 1, "verifications": 2, "pushes": 2,
                                                 "prs_opened": 1, "ci_results": 2, "ci_reruns": 1})
        self.assertIn("without a model: 2 gate runs, 1 setup commands", status.render(30))
        self.assertEqual(status.snapshot(30)["model_free"]["gates"], 2)

    def test_kill_switch_render_and_json(self):
        paths.kill_file().parent.mkdir(parents=True, exist_ok=True)
        paths.kill_file().write_text("now")
        self.assertTrue(json.loads(status.as_json(30))["kill_switch"])
        text = status.render(30)
        self.assertIn("kill switch: ON", text)
        self.assertIn("waiting for your approval: 1", text)
        self.assertIn("make deploy", text)

    def test_a_running_task_without_its_runner_is_reported_killed(self):
        t = tasks.create("Orphan", "x", type="code", repo="r1", status="running", mode="smart")
        s = status.snapshot(30)
        self.assertEqual(next(r for r in s["tasks"] if r["id"] == t.id)["status"], "killed")
        self.assertEqual(s["running"], [])


    def test_no_decision_while_the_kill_switch_is_on(self):
        # the dashboard's approve button runs `sadyanx approve`: with everything stopped it must not
        # push, open a PR, send on Slack or resume the agent
        paths.kill_file().parent.mkdir(parents=True, exist_ok=True)
        paths.kill_file().write_text("now")
        aid = approvals.pending(paths.run_dir(self.parked.id))[0]["id"]
        for verb in ("approve", "reject"):
            with self.subTest(verb=verb), self.assertRaises(SystemExit) as exit_, \
                    contextlib.redirect_stderr(io.StringIO()) as err:
                cli.main([verb, self.parked.id, aid])
            self.assertEqual(exit_.exception.code, 1)
            self.assertIn("kill switch", err.getvalue())
        self.assertEqual([i["id"] for i in approvals.pending(paths.run_dir(self.parked.id))], [aid])

    def test_an_unknown_approval_id_is_refused(self):
        # the app always names the exact approval it showed; one decided meanwhile is not re-decided
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()) as err:
            cli.main(["approve", self.parked.id, "deadbeef"])
        self.assertIn("no pending approval with id: deadbeef", err.getvalue())
        self.assertEqual(len(approvals.pending(paths.run_dir(self.parked.id))), 1)


if __name__ == "__main__":
    unittest.main()
