import io
import json
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sadyanxlib import mcp_server  # noqa: E402


class FakeRun:
    """Stands in for subprocess.run: records the argument list, answers like the CLI."""

    def __init__(self, code=0, stdout='{"ok": true}', stderr=""):
        self.calls, self.code, self.stdout, self.stderr = [], code, stdout, stderr

    def __call__(self, args, **kw):
        self.calls.append((args, kw))
        return subprocess.CompletedProcess(args, self.code, self.stdout, self.stderr)


def call(name, arguments, env=None, run=None):
    msg = {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
    res = mcp_server.handle(msg, env or {}, run or FakeRun())["result"]
    return res["isError"], res["content"][0]["text"]


class TestMcpServer(unittest.TestCase):
    def test_handshake_and_tools(self):
        init = mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                  "params": {"protocolVersion": "2025-03-26"}}, {})
        self.assertEqual(init["result"]["protocolVersion"], "2025-03-26")
        self.assertIsNone(mcp_server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}, {}))
        tools = {t["name"]: t for t in mcp_server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, {})
                 ["result"]["tools"]}
        self.assertEqual(set(tools), {"status", "approve", "reject"})
        self.assertTrue(tools["status"]["annotations"]["readOnlyHint"])
        self.assertFalse(tools["approve"]["annotations"]["readOnlyHint"])  # the app asks before running it
        err = mcp_server.handle({"jsonrpc": "2.0", "id": 3, "method": "resources/list"}, {})
        self.assertEqual(err["error"]["code"], -32601)

    def test_status_runs_the_cli_without_a_shell(self):
        run = FakeRun()
        self.assertEqual(call("status", {"days": 7}, run=run), (False, '{"ok": true}'))
        args, kw = run.calls[0]
        self.assertEqual(args[-4:], ["status", "--json", "--days", "7"])
        self.assertNotIn("shell", kw)
        for bad in (0, 400, "7", True, 2.5):
            with self.subTest(days=bad):
                self.assertTrue(call("status", {"days": bad})[0])

    def test_status_must_be_json(self):
        err, text = call("status", {}, run=FakeRun(stdout="Sady: hi"))
        self.assertTrue(err)
        self.assertIn("did not print JSON", text)

    def test_approve_and_reject_name_one_approval(self):
        run = FakeRun(stdout="approved: [3f9a1c2e] deploy\ntask 0005 running in the background\n")
        err, text = call("approve", {"task": "0005", "approval": "3f9a1c2e"}, run=run)
        self.assertFalse(err)
        self.assertIn("running in the background", text)
        self.assertEqual(run.calls[0][0][-3:], ["approve", "0005", "3f9a1c2e"])
        run = FakeRun(stdout="rejected")
        reason = "not on Friday's deploy; $(rm -rf ~) `id`"
        self.assertFalse(call("reject", {"task": "0005", "approval": "3f9a1c2e", "reason": reason}, run=run)[0])
        self.assertEqual(run.calls[0][0][-5:], ["reject", "0005", "3f9a1c2e", "--reason", reason])  # one argument

    def test_bad_arguments_never_reach_the_cli(self):
        run = FakeRun()
        bad = [
            ("approve", {"task": "0005; rm -rf /", "approval": "3f9a1c2e"}),
            ("approve", {"task": "0005", "approval": "3F9A1C2E"}),
            ("approve", {"task": "0005", "approval": "3f9a1c2e0"}),
            ("approve", {"task": "0005"}),
            ("approve", {"task": "0005", "approval": "3f9a1c2e", "all": True}),       # never --all
            ("approve", {"task": "0005", "approval": "3f9a1c2e", "reason": "x"}),     # approve takes no reason
            ("reject", {"task": "0005", "approval": "3f9a1c2e", "reason": "x" * 301}),
            ("reject", {"task": "0005", "approval": "3f9a1c2e", "reason": ["x"]}),
            ("run", {"task": "0005"}),
            ("kill", {}),
        ]
        for name, args in bad:
            with self.subTest(name=name, args=args):
                self.assertTrue(call(name, args, run=run)[0])
        self.assertEqual(run.calls, [])

    def test_cli_failures_are_tool_errors(self):
        run = FakeRun(code=1, stderr="sadyanx: the kill switch is on: nothing is approved or rejected\n")
        err, text = call("approve", {"task": "0005", "approval": "3f9a1c2e"}, run=run)
        self.assertTrue(err)
        self.assertTrue(text.startswith("the kill switch is on"))

    def test_refuses_everything_inside_an_agent_session(self):
        run = FakeRun()
        for env in ({"SADYANX_GUARD_CTX": "/x/guard.json"}, {"SADYANX_TASK_ID": "0005"}):
            for name, args in (("status", {}), ("approve", {"task": "0005", "approval": "3f9a1c2e"})):
                with self.subTest(env=env, name=name):
                    err, text = call(name, args, env=env, run=run)
                    self.assertTrue(err)
                    self.assertIn("agent session", text)
        self.assertEqual(run.calls, [])

    def test_serve_speaks_one_json_per_line(self):
        lines = "\n".join([
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
            "not json",
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
            "",
        ]) + "\n"
        out = io.StringIO()
        mcp_server.serve({}, io.StringIO(lines), out)
        replies = [json.loads(x) for x in out.getvalue().splitlines()]
        self.assertEqual(replies[0], {"jsonrpc": "2.0", "id": 1, "result": {}})
        self.assertEqual(replies[1]["error"]["code"], -32700)
        self.assertEqual(len(replies), 2)


if __name__ == "__main__":
    unittest.main()
