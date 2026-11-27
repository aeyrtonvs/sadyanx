import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sadyanxlib import approvals, cli, paths, tasks, timeline  # noqa: E402


class TestTimeline(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        os.environ["SADYANX_ROOT"] = self.root.name
        self.task = tasks.create("Add retries", "x", type="code", repo="r1", status="done", mode="smart")
        self.dir = paths.run_dir(self.task.id)
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "events.log").write_text("\n".join([
            "[2026-11-20T10:00:00] === task 0001: Add retries",
            "[2026-11-20T10:00:00] worktree /tmp/wt on branch feature/x",
            "[2026-11-20T10:00:01] agent: starting session abc (mode smart: opus/high)",
            "[2026-11-20T10:05:00] gate: make test",
            "[2026-11-20T10:05:30] verification failed: 1 problem(s)",
            "  - make test: 2 failures",
            "[2026-11-20T10:06:00] attempt 2: escalating smart/high to smart/max",
            "[2026-11-20T10:20:00] verification passed",
            "[2026-11-20T10:21:00] PR waiting for approval [3f9a1c2e]",
            "[2026-11-20T10:30:00] PR opened: https://example.com/pr/1",
            "[2026-11-20T10:40:00] CI: AWS CodeBuild failed on 50a9a56e: lint",
            "[2026-11-20T10:50:00] error: setup 'make setup' failed",
            "[2026-11-20T11:00:00] final status: done PR ready for your review",
            "[2026-11-20T11:00:01] something new the runner says",
            "not a line of its own, and no event above it on purpose? no: it follows the last one",
        ]) + "\n")
        (self.dir / "usage.jsonl").write_text("\n".join([
            json.dumps({"ts": "2026-11-20T10:05:00", "task": "0001", "phase": "work", "attempt": 1, "mode": "smart",
                        "model": "opus", "effort": "high", "outcome": "done", "seconds": 290, "cost_usd": 1.25,
                        "tokens": {"input": 10, "output": 90, "cache_read": 900, "cache_write": 0}}),
            "not json",
        ]) + "\n")
        (self.dir / "audit.jsonl").write_text("\n".join(json.dumps(r) for r in [
            {"ts": "2026-11-20T10:03:00", "tool": "Bash", "verdict": "deny", "category": "secrets", "reason": ".env"},
            {"ts": "2026-11-20T10:03:01", "tool": "Bash", "verdict": "allow"},
            {"ts": "2026-11-20T10:03:02", "tool": "Bash", "verdict": "ask", "category": "deploy"},
        ]) + "\n")
        item = approvals.request_harness(self.dir, "open-pr", {"title": "Add retries"}, "pr", "opening a PR")
        approvals.decide(self.dir, [item["id"]], "approved")

    def tearDown(self):
        os.environ.pop("SADYANX_ROOT", None)
        self.root.cleanup()

    def test_merges_every_source_in_order(self):
        t = timeline.build(self.task.id)
        ev = t["events"]
        self.assertEqual([e["ts"] for e in ev], sorted(e["ts"] for e in ev))
        kinds = [e["kind"] for e in ev if e["ts"].startswith("2026-11-20T10:0")]
        self.assertEqual(kinds[:6], ["start", "start", "agent", "guard", "gate", "call"])
        call = next(e for e in ev if e["kind"] == "call")
        self.assertEqual((call["tokens"], call["cost_usd"], call["seconds"]), (1000, 1.25, 290))
        self.assertIn("work call, attempt 1 (smart · opus/high): done", call["text"])
        self.assertEqual([e["text"] for e in ev if e["kind"] == "guard"], ["guard denied Bash (secrets): .env"])
        self.assertFalse(t["truncated"])

    def test_tones_details_and_unknown_lines(self):
        ev = timeline.build(self.task.id)["events"]
        by = {e["text"].split(":")[0]: e for e in ev}
        self.assertEqual(by["verification failed"]["tone"], "bad")
        self.assertEqual(by["verification failed"]["detail"], "  - make test: 2 failures")
        self.assertEqual(by["verification passed"]["tone"], "good")
        self.assertEqual(by["PR opened"]["kind"], "ship")
        self.assertEqual((by["CI"]["kind"], by["CI"]["tone"]), ("ci", "bad"))
        self.assertEqual((by["error"]["kind"], by["error"]["tone"]), ("error", "bad"))
        self.assertEqual((by["final status"]["kind"], by["final status"]["tone"]), ("final", "good"))
        last = ev[-1] if ev[-1]["kind"] == "note" else next(e for e in ev if e["kind"] == "note")
        self.assertEqual(last["text"], "something new the runner says")
        self.assertIn("it follows the last one", last["detail"])

    def test_approvals_show_once_with_their_decision(self):
        ev = timeline.build(self.task.id)["events"]
        self.assertFalse(any("waiting for approval" in e["text"] for e in ev))
        parked = [e for e in ev if e["kind"] == "approval"]
        self.assertEqual(len(parked), 2)
        self.assertTrue(parked[0]["text"].startswith("parked for your approval: ["))
        self.assertEqual((parked[1]["tone"], parked[1]["text"][:12]), ("good", "you approved"))

    def test_long_histories_keep_the_latest(self):
        lines = [f"[2026-11-21T{h:02d}:{m:02d}:00] gate: make test" for h in range(10) for m in range(60)]
        (self.dir / "events.log").write_text("\n".join(lines) + "\n")
        t = timeline.build(self.task.id)
        self.assertTrue(t["truncated"])
        self.assertEqual(len(t["events"]), timeline.MAX_EVENTS)
        gates = [e for e in t["events"] if e["kind"] == "gate"]
        self.assertEqual(gates[-1]["ts"], "2026-11-21T09:59:00")
        self.assertNotIn("2026-11-21T00:00:00", [e["ts"] for e in gates])  # the oldest were left out

    def test_a_task_that_never_ran_has_an_empty_timeline(self):
        other = tasks.create("Not yet", "x", type="code", repo="r1", status="ready", mode="smart")
        self.assertEqual(timeline.build(other.id), {"task": other.id, "events": [], "truncated": False})

    def test_cli_prints_it_as_json(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            cli.main(["log", self.task.id, "--json"])
        data = json.loads(out.getvalue())
        self.assertEqual((data["task"], data["title"]), (self.task.id, "Add retries"))
        self.assertTrue(data["events"])


if __name__ == "__main__":
    unittest.main()
