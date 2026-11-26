import os
import random
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sadyanxlib import notify, paths, persona, status, tasks  # noqa: E402

GOOD = '''
name = "Sady"
language = "English"
voice = """
Dry.
Short.
"""
[lines]
done = ["$id done: $title", "Finished $nothing_here"]
waiting = "$n waiting"
idle = []
'''


class TestPersona(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.state = tempfile.TemporaryDirectory()
        os.environ["SADYANX_ROOT"] = self.root.name
        os.environ["SADYANX_STATE"] = self.state.name
        paths.config_dir().mkdir(parents=True)

    def tearDown(self):
        os.environ.pop("SADYANX_ROOT", None)
        os.environ.pop("SADYANX_STATE", None)
        self.root.cleanup()
        self.state.cleanup()

    def write(self, text: str) -> None:
        (paths.config_dir() / persona.FILENAME).write_text(text, encoding="utf-8")

    def test_missing_disabled_nameless_or_broken_means_no_persona(self):
        self.assertIsNone(persona.load())
        self.assertEqual(persona.line("done", id="0001"), "")
        self.assertIn("no persona", persona.describe())
        for text in ("enabled = false\n" + GOOD, 'name = ""\n', "name = \"Sady\"\n[lines\n"):
            with self.subTest(text=text[-30:]):
                self.write(text)
                self.assertIsNone(persona.load())

    def test_lines_fill_placeholders_and_leave_unknown_ones(self):
        self.write(GOOD)
        p = persona.load()
        self.assertEqual((p.name, p.language, p.voice), ("Sady", "English", "Dry.\nShort."))
        rng = random.Random(0)
        seen = {p.line("done", rng=rng, id="0007", title="Fix   it") for _ in range(30)}
        self.assertEqual(seen, {"0007 done: Fix it", "Finished $nothing_here"})
        self.assertEqual(p.line("waiting", n=2), "2 waiting")  # a single string works as a list of one
        self.assertEqual(p.line("idle"), "")                   # no lines: nothing added
        self.assertEqual(p.line("unknown-event"), "")

    def test_long_lines_are_cut(self):
        self.write('name = "S"\n[lines]\ndone = ["' + "x" * 400 + '"]\n')
        out = persona.line("done")
        self.assertEqual(len(out), persona.MAX_LINE)
        self.assertTrue(out.endswith("…"))

    def test_describe_is_what_task_reads(self):
        self.write(GOOD)
        self.assertEqual(persona.describe(), "name: Sady\nlanguage: English\nvoice:\n  Dry.\n  Short.")

    def test_status_carries_the_mood_line(self):
        self.write(GOOD)
        s = status.snapshot(30)
        self.assertEqual(s["persona"], {"name": "Sady", "line": ""})  # idle has no lines
        t = tasks.create("Parked", "x", type="code", repo="r", status="needs-approval", mode="smart")
        from sadyanxlib import approvals
        approvals.request_tool(paths.run_dir(t.id), "Bash", {"command": "make deploy"}, "deploy", "r")
        self.assertEqual(status.snapshot(30)["persona"]["line"], "1 waiting")
        self.assertTrue(status.render(30).startswith("Sady: 1 waiting\n"))

    def test_mood_order(self):
        self.assertEqual(status.mood(True, 3, 1), ("kill-switch", 0))
        self.assertEqual(status.mood(False, 3, 1), ("waiting", 3))
        self.assertEqual(status.mood(False, 0, 2), ("working", 2))
        self.assertEqual(status.mood(False, 0, 0), ("idle", 0))

    def test_no_persona_status_has_none(self):
        self.assertIsNone(status.snapshot(30)["persona"])
        self.assertTrue(status.render(30).startswith("kill switch:"))


class TestNotifyScript(unittest.TestCase):
    def test_subtitle_is_quoted_and_optional(self):
        plain = notify.macos_script("✅ 0001 done", "Title", sound="Glass")
        self.assertNotIn("subtitle", plain)
        s = notify.macos_script("t", "m", subtitle='say "hi" \\ bye')
        self.assertIn('subtitle "say \\"hi\\" \\\\ bye"', s)


if __name__ == "__main__":
    unittest.main()
