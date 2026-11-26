import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sadyanxlib import approvals, config, paths, report, runner, slack, tasks  # noqa: E402

FMT = {**slack.SLACK_DEFAULTS, "greeting": "Hola!", "sign_off": "Saludos.", "language": "Spanish"}

BODY = """## Context

Check a flag in the production config.

## Acceptance criteria

- [ ] The flag's value is reported

## Ticket (verbatim)

Can you check the flag, please?
"""

RESULT = {"status": "done", "summary": "s", "links": ["https://claude.ai/artifact/x"],
          "criteria": [{"criterion": "The flag's value is reported", "met": True, "evidence": "report.md"}],
          "reply": "**Flag:** it was off; it is on now.\n\n1. Can you confirm the release time?"}

# A stand-in for the send session: it records its argv and, like the real guard would, writes
# an audit line for the pinned call (unless told not to), then prints a result.
FAKE_SEND = r"""#!/usr/bin/env python3
import json, os, pathlib, sys
here = pathlib.Path(__file__).parent
(here / "send-args.json").write_text(json.dumps(sys.argv[1:]))
ctx = json.loads(pathlib.Path(os.environ["SADYANX_GUARD_CTX"]).read_text())
plan = json.loads((here / "send-plan.json").read_text())
if plan["audit"]:
    with open(pathlib.Path(ctx["run_dir"]) / "audit.jsonl", "a") as fh:
        fh.write(json.dumps({"verdict": "allow", "category": "pinned", "tool": "x"}) + "\n")
print(json.dumps({"type": "result", "subtype": "success", "num_turns": 2, "total_cost_usd": 0.01,
                  "usage": {"input_tokens": 500, "output_tokens": 40}, "structured_output": plan["verdict"]}))
"""


class TestOrigin(unittest.TestCase):
    def test_direct_message_link_replies_in_the_conversation(self):
        link = "https://example.slack.com/archives/D0123ABCDE/p1700000000123456"
        self.assertEqual(slack.parse_origin(link), ("D0123ABCDE", ""))

    def test_channel_link_replies_in_the_message_thread(self):
        link = "https://example.slack.com/archives/C0456FGHIJ/p1700000000123456"
        self.assertEqual(slack.parse_origin(link), ("C0456FGHIJ", "1700000000.123456"))

    def test_a_message_inside_a_thread_replies_in_that_thread(self):
        link = ("https://example.slack.com/archives/C0456FGHIJ/p1700000999000100"
                "?thread_ts=1700000000.123456&cid=C0456FGHIJ")
        self.assertEqual(slack.parse_origin(link), ("C0456FGHIJ", "1700000000.123456"))
        dm = "https://example.slack.com/archives/D0123ABCDE/p1700000999000100?thread_ts=1700000000.123456"
        self.assertEqual(slack.parse_origin(dm), ("D0123ABCDE", "1700000000.123456"))

    def test_plain_ids(self):
        self.assertEqual(slack.parse_origin("D0123ABCDE"), ("D0123ABCDE", ""))
        self.assertEqual(slack.parse_origin("C0456FGHIJ/1700000000.123456"), ("C0456FGHIJ", "1700000000.123456"))
        self.assertEqual(slack.parse_origin("C0456FGHIJ:1700000000.123456"), ("C0456FGHIJ", "1700000000.123456"))

    def test_bad_origins_are_refused(self):
        for value in ["", "general", "https://example.slack.com/team/U123", "C0456FGHIJ/123",
                      "https://example.slack.com/archives/C0456FGHIJ/x123", "U0789KLMNO",
                      "https://example.slack.com/archives/C0456FGHIJ/p1700000000123456?thread_ts=abc"]:
            with self.subTest(value=value):
                with self.assertRaises(slack.OriginError):
                    slack.parse_origin(value)


class TestFormat(unittest.TestCase):
    def test_greeting_body_sign_off(self):
        self.assertEqual(slack.render("Ya quedó.\n\n- uno", FMT), "Hola!\n\nYa quedó.\n\n- uno\n\nSaludos.")

    def test_a_greeting_or_sign_off_the_agent_wrote_is_not_doubled(self):
        for body in ["Hola!\n\nYa quedó.\n\nSaludos.", "hola\nYa quedó.\nSaludos", "  ¡Hola!  \n\nYa quedó.\n\n"]:
            with self.subTest(body=body):
                self.assertEqual(slack.render(body, FMT), "Hola!\n\nYa quedó.\n\nSaludos.")
        # a line that only starts like the greeting stays
        self.assertEqual(slack.render("Hola equipo, ya quedó.", FMT), "Hola!\n\nHola equipo, ya quedó.\n\nSaludos.")

    def test_empty_or_long_bodies_are_not_sent(self):
        self.assertIsNotNone(slack.problem("", FMT))
        self.assertIsNotNone(slack.problem("   \n", FMT))
        self.assertIsNotNone(slack.problem("x" * (FMT["max_chars"] + 1), FMT))
        self.assertIsNone(slack.problem("Ya quedó.", FMT))

    def test_send_args_leave_out_an_empty_thread(self):
        self.assertEqual(slack.send_args("D1", "", "t"), {"channel_id": "D1", "message": "t"})
        self.assertEqual(slack.send_args("C1", "1.2", "t"), {"channel_id": "C1", "message": "t", "thread_ts": "1.2"})


class Env(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        os.environ["SADYANX_ROOT"] = str(root / "root")
        os.environ["SADYANX_STATE"] = str(root / "state")
        (root / "root" / "config").mkdir(parents=True)
        self.root = root

    def tearDown(self):
        os.environ.pop("SADYANX_ROOT", None)
        os.environ.pop("SADYANX_STATE", None)
        self.tmp.cleanup()

    def write_config(self, text: str) -> None:
        (self.root / "root" / "config" / "sadyanx.toml").write_text(text)


class TestConfig(Env):
    def test_defaults_and_overrides(self):
        self.assertEqual(config.load().slack["greeting"], "Hi!")
        self.write_config('[slack]\ngreeting = "Hola!"\nsign_off = "Saludos."\n')
        cfg = config.load()
        self.assertEqual((cfg.slack["greeting"], cfg.slack["sign_off"]), ("Hola!", "Saludos."))

    def test_bad_values_fail_at_load(self):
        for text in ['[slack]\ngreting = "Hola!"\n', '[slack]\ngreeting = ""\n', '[slack]\nmax_chars = 99999\n',
                     '[slack]\nmax_chars = "lots"\n', '[slack]\nsend_tool = 3\n']:
            with self.subTest(text=text):
                self.write_config(text)
                with self.assertRaises(config.ConfigError):
                    config.load()


class TestAdd(Env):
    def add(self, *args: str) -> subprocess.CompletedProcess:
        bin_ = str(Path(__file__).resolve().parents[1] / "bin" / "sadyanx")
        return subprocess.run([sys.executable, bin_, "add", "--type", "research", "--title", "Check the flag",
                               "--body", BODY, *args], capture_output=True, text=True, env=os.environ)

    def test_the_origin_is_stored(self):
        out = self.add("--slack", "https://example.slack.com/archives/C0456FGHIJ/p1700000000123456")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("reply on Slack", out.stdout)
        t = tasks.find(out.stdout.split()[0])
        self.assertEqual((t.get("slack_channel"), t.get("slack_thread")), ("C0456FGHIJ", "1700000000.123456"))

    def test_a_bad_origin_creates_nothing(self):
        out = self.add("--slack", "not-a-link")
        self.assertNotEqual(out.returncode, 0)
        self.assertEqual(list(paths.tasks_dir().glob("*.md")), [])


class TestReplyFlow(Env):
    """The task is done: the reply is formatted, shown to you and sent only on your yes."""

    def setUp(self):
        super().setUp()
        self.write_config('[slack]\ngreeting = "Hola!"\nsign_off = "Saludos."\nlanguage = "Spanish"\n')
        self.claude = self.root / "claude"
        self.claude.write_text(FAKE_SEND)
        self.claude.chmod(0o755)
        self.task = tasks.create("Check the flag", BODY, type="research", status="ready",
                                 slack_channel="D0123ABCDE", slack_thread="")
        self.cfg = config.load()
        self.cfg.defaults["claude_bin"] = str(self.claude)
        self.cfg.notify["macos"] = False
        self.print = mock.patch("builtins.print")
        self.print.start()

    def tearDown(self):
        self.print.stop()
        super().tearDown()

    def send_plan(self, sent: bool = True, audit: bool = True) -> None:
        verdict = {"sent": sent, "link": "https://example.slack.com/archives/D0123ABCDE/p1" if sent else "",
                   "note": "posted" if sent else "the tool failed"}
        (self.root / "send-plan.json").write_text(json.dumps({"verdict": verdict, "audit": audit}))

    def runner_for(self, run_mode: str) -> runner.Runner:
        return runner.Runner(tasks.find(self.task.id), self.cfg, run_mode=run_mode)

    def test_the_prompt_asks_for_a_reply_only_when_the_task_came_from_slack(self):
        r = self.runner_for("autonomous")
        r.workdir.mkdir(parents=True)
        prompt = r.initial_prompt()
        self.assertIn("## Reply to whoever asked", prompt)
        self.assertIn("Spanish", prompt)
        self.assertIn('"Hola!"', prompt)
        plain = tasks.create("Other", BODY, type="research", status="ready")
        self.assertNotIn("Reply to whoever asked", runner.Runner(plain, self.cfg).initial_prompt())

    def test_a_reply_must_be_a_string(self):
        self.assertIsNone(runner.check_result(RESULT))
        self.assertIn("reply", runner.check_result({**RESULT, "reply": ["x"]}))

    def test_autonomous_parks_the_exact_message_for_approval(self):
        r = self.runner_for("autonomous")
        status = r.finish("done", "", RESULT)
        self.assertEqual(status, "needs-approval")
        t = tasks.find(self.task.id)
        self.assertEqual((t.status, t.get("phase"), t.get("slack_reply")), ("needs-approval", "reply",
                                                                           "waiting for approval"))
        [item] = approvals.pending(r.run_dir)
        expected = "Hola!\n\n" + RESULT["reply"] + "\n\nSaludos."
        self.assertEqual(item["payload"]["text"], expected)
        self.assertIn("    1. Can you confirm the release time?", approvals.describe(item))
        self.assertEqual((r.run_dir / runner.REPLY_FILE).read_text(), expected + "\n")
        self.assertFalse((self.root / "send-args.json").exists())  # nothing sent yet
        with self.assertRaises(runner.RunError):
            self.runner_for("autonomous")._run()

    def test_approving_sends_it_and_finishes_done(self):
        self.runner_for("autonomous").finish("done", "flag on", RESULT)
        approvals.decide(paths.run_dir(self.task.id), None, "approved")
        self.send_plan()
        final = runner.apply_harness_approvals(tasks.find(self.task.id), self.cfg)
        self.assertEqual(final, "done")
        t = tasks.find(self.task.id)
        self.assertEqual(t.get("slack_reply"), "https://example.slack.com/archives/D0123ABCDE/p1")
        self.assertIn("flag on; Slack reply sent", t.get("reason"))
        self.assertEqual(t.get("phase"), "work")
        args = json.loads((self.root / "send-args.json").read_text())
        self.assertEqual(args[args.index("--tools") + 1], "ToolSearch")
        ctx = json.loads((paths.run_dir(self.task.id) / "guard-slack.json").read_text())
        self.assertTrue(ctx["deny_gated"])
        self.assertEqual(ctx["write_roots"], [])
        self.assertEqual(ctx["pinned_calls"][0]["input"],
                         {"channel_id": "D0123ABCDE", "message": "Hola!\n\n" + RESULT["reply"] + "\n\nSaludos."})
        self.assertIn("Slack reply", report.write(t, paths.run_dir(t.id), RESULT).read_text())

    def test_rejecting_sends_nothing(self):
        self.runner_for("autonomous").finish("done", "", RESULT)
        approvals.decide(paths.run_dir(self.task.id), None, "rejected", "not now")
        self.assertEqual(runner.apply_harness_approvals(tasks.find(self.task.id), self.cfg), "done")
        t = tasks.find(self.task.id)
        self.assertEqual(t.get("slack_reply"), "not sent (rejected)")
        self.assertFalse((self.root / "send-args.json").exists())

    def test_a_session_that_claims_it_sent_without_the_pinned_call_did_not_send(self):
        self.send_plan(sent=True, audit=False)
        note = self.runner_for("autonomous").send_slack({"channel": "D0123ABCDE", "thread": "", "text": "Hola!"})
        self.assertIn("NOT sent", note)
        self.assertTrue(tasks.find(self.task.id).get("slack_reply").startswith("not sent"))

    def test_supervised_asks_in_the_terminal(self):
        with mock.patch.object(runner, "has_terminal", return_value=True), \
                mock.patch.object(runner.Runner, "confirm", return_value=False) as ask:
            status = self.runner_for("supervised").finish("done", "", RESULT)
        self.assertEqual(status, "done")
        self.assertIn("Slack", ask.call_args.args[0])
        self.assertFalse(ask.call_args.kwargs.get("default", True))  # default no
        t = tasks.find(self.task.id)
        self.assertEqual(t.get("slack_reply"), "not sent (your call)")
        self.assertEqual(approvals.load(paths.run_dir(t.id)), [])
        self.assertFalse((self.root / "send-args.json").exists())

    def test_supervised_yes_sends(self):
        self.send_plan()
        with mock.patch.object(runner, "has_terminal", return_value=True), \
                mock.patch.object(runner.Runner, "confirm", return_value=True):
            status = self.runner_for("supervised").finish("done", "", RESULT)
        self.assertEqual(status, "done")
        self.assertIn("Slack reply sent", tasks.find(self.task.id).get("reason"))

    def test_no_reply_drafted_finishes_done_without_asking(self):
        status = self.runner_for("autonomous").finish("done", "", {**RESULT, "reply": ""})
        self.assertEqual(status, "done")
        t = tasks.find(self.task.id)
        self.assertIn("no Slack reply", t.get("reason"))
        self.assertEqual(approvals.load(paths.run_dir(t.id)), [])

    def test_failed_and_blocked_tasks_send_nothing(self):
        for status in ("failed", "blocked"):
            with self.subTest(status=status):
                self.assertEqual(self.runner_for("autonomous").finish(status, "x", RESULT), status)
                self.assertEqual(approvals.load(paths.run_dir(self.task.id)), [])

    def test_a_task_without_a_slack_origin_never_replies(self):
        plain = tasks.create("Other", BODY, type="research", status="ready")
        self.assertEqual(runner.Runner(plain, self.cfg, run_mode="autonomous").finish("done", "", RESULT), "done")
        self.assertEqual(approvals.load(paths.run_dir(plain.id)), [])


if __name__ == "__main__":
    unittest.main()
