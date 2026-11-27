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

    def test_spend_against_budget_and_peers(self):
        def row(tid, cost, status_="done", run="autonomous", budget=10.0, mode="smart", type_="code"):
            return {"id": tid, "status": status_, "type": type_, "mode": mode, "cost_usd": cost,
                    "budget_usd": budget, "run": run}
        rows = [row("1", 1.0), row("2", 1.2), row("3", 1.4), row("4", 1.6),
                row("5", 3.5, "running"),                    # 2.9x the median of four finished peers
                row("6", 8.5, "running"),                    # past 80% of its own budget
                row("7", 9.5, "running", run="supervised"),  # no dollar cap while you watch
                row("8", 0.4, "running"),                    # 2x the median, but under a dollar
                row("9", 3.0, mode="cheap"),                 # no peers of its mode
                row("10", 0.0, "ready", run="")]             # never ran
        status.spend(rows)
        by = {r["id"]: r for r in rows}
        self.assertEqual((by["5"]["cost_flag"], by["5"]["peer_median_usd"], by["5"]["peers"]), ("peers", 1.3, 4))
        self.assertEqual((by["6"]["cost_flag"], by["6"]["budget_used"]), ("budget", 0.85))
        self.assertIsNone(by["7"]["budget_used"])
        self.assertEqual(by["7"]["cost_flag"], "peers")  # still far above its peers
        self.assertEqual(by["8"]["cost_flag"], "")
        self.assertEqual((by["9"]["cost_flag"], by["9"]["peer_median_usd"]), ("", None))
        self.assertEqual((by["10"]["cost_flag"], by["10"]["budget_used"]), ("", 0.0))
        self.assertEqual(by["1"]["peers"], 3)  # a task is never its own peer

    def test_run_kind_comes_from_the_last_guard_context(self):
        self.assertEqual(status.run_kind(self.done.id), "")
        d = paths.run_dir(self.done.id)
        d.mkdir(parents=True, exist_ok=True)
        (d / "guard.json").write_text(json.dumps({"interactive": True}))
        self.assertEqual(status.run_kind(self.done.id), "supervised")
        (d / "guard.json").write_text(json.dumps({"interactive": False}))
        self.assertEqual(status.run_kind(self.done.id), "autonomous")
        (d / "guard.json").write_text("not json")
        self.assertEqual(status.run_kind(self.done.id), "")
        row = next(r for r in status.snapshot(30)["tasks"] if r["id"] == self.done.id)
        self.assertIn("cost_flag", row)
        self.assertIn("weekly", status.snapshot(30))

    def test_weekly_compares_the_last_seven_days_with_the_seven_before(self):
        import datetime as dt
        now = dt.datetime(2026, 11, 26, 12, 0, 0)
        ago = lambda days: (now - dt.timedelta(days=days)).isoformat()  # noqa: E731
        tok = lambda n: {"input": 0, "output": n, "cache_read": 0, "cache_write": 0}  # noqa: E731
        recs = [
            {"ts": ago(30), "task": "A", "phase": "work", "tokens": tok(500)},   # older: counts for A only
            {"ts": ago(2), "task": "A", "phase": "fix", "tokens": tok(100)},
            {"ts": ago(3), "task": "B", "phase": "work", "tokens": tok(200)},
            {"ts": ago(9), "task": "C", "phase": "work", "tokens": tok(300)},
            {"ts": ago(10), "task": "C", "phase": "fix", "tokens": tok(50)},
        ]
        rows = [{"id": "A", "status": "done", "updated": ago(1)},
                {"id": "B", "status": "done", "updated": ago(2)},
                {"id": "C", "status": "done", "updated": ago(8)},
                {"id": "D", "status": "failed", "updated": ago(1)},
                {"id": "E", "status": "done", "updated": ago(20)}]
        audit = [("A", {"ts": ago(1), "verdict": "deny"}), ("A", {"ts": ago(1), "verdict": "allow"}),
                 ("C", {"ts": ago(8), "verdict": "deny"}), ("C", {"ts": ago(9), "verdict": "deny"})]
        w = status.weekly(recs, rows, audit, now)
        self.assertEqual(w["current"], {"tokens": 300, "finished": 2, "first_pass_pct": 50,
                                        "tokens_per_finished": 400, "fix_calls": 1, "denials": 1})
        self.assertEqual(w["previous"], {"tokens": 350, "finished": 1, "first_pass_pct": 0,
                                         "tokens_per_finished": 350, "fix_calls": 1, "denials": 2})
        empty = status.weekly([], [], [], now)["current"]
        self.assertEqual((empty["first_pass_pct"], empty["tokens_per_finished"]), (None, None))

    def test_render_names_spend_worth_a_look(self):
        self.done.set(budget_usd=10, cost_usd=9)
        self.done.save()
        self.assertIn(f"$ {self.done.id} spent $9.00 of its $10.00 budget", status.render(30))

    def test_an_unknown_approval_id_is_refused(self):
        # the app always names the exact approval it showed; one decided meanwhile is not re-decided
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()) as err:
            cli.main(["approve", self.parked.id, "deadbeef"])
        self.assertIn("no pending approval with id: deadbeef", err.getvalue())
        self.assertEqual(len(approvals.pending(paths.run_dir(self.parked.id))), 1)


if __name__ == "__main__":
    unittest.main()
