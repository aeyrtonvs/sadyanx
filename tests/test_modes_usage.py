import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sadyanxlib import config, modes, tasks, usage  # noqa: E402
from sadyanxlib.runner import skill_meta, skills_section  # noqa: E402


class TestModes(unittest.TestCase):
    def test_normalize_and_aliases(self):
        self.assertEqual(modes.normalize("!deep"), "ultra")
        self.assertEqual(modes.normalize("fast"), "balanced")
        self.assertEqual(modes.normalize(" Smart "), "smart")
        with self.assertRaises(ValueError):
            modes.normalize("turbo")

    @staticmethod
    def steps(mode: str, n: int = 3, **kw) -> list[str]:
        return [f"{m.name}/{m.effort}" for m in (modes.for_attempt(mode, {}, a, **kw) for a in range(1, n + 1))]

    def test_escalation_raises_effort_before_the_model(self):
        self.assertEqual(self.steps("smart"), ["smart/high", "smart/xhigh", "smart/max"])
        self.assertEqual(self.steps("smart", escalate=False), ["smart/high"] * 3)

    def test_ultra_is_never_reached_by_default(self):
        self.assertEqual(self.steps("smart", 6), ["smart/high", "smart/xhigh", "smart/max"] + ["smart/max"] * 3)
        # raising the ceiling lets the ladder move on, starting the next mode at its own effort
        self.assertEqual(self.steps("smart", 5, ceiling="ultra")[3:], ["ultra/xhigh", "ultra/max"])
        # a mode chosen above the ceiling is still used (you chose it), and climbs its effort
        self.assertEqual(self.steps("ultra"), ["ultra/xhigh", "ultra/max", "ultra/max"])

    def test_lower_modes_move_up_a_model_after_max(self):
        ladder = [f"{m.name}/{m.effort}" for m in modes.ladder("cheap", {})]
        self.assertEqual(ladder, ["cheap/low", "cheap/medium", "cheap/high", "cheap/xhigh", "cheap/max",
                                  "balanced/medium", "balanced/high", "balanced/xhigh", "balanced/max",
                                  "smart/high", "smart/xhigh", "smart/max"])

    def test_effort_override_and_rerun_start(self):
        self.assertEqual(self.steps("smart", effort="xhigh"), ["smart/xhigh", "smart/max", "smart/max"])
        self.assertEqual(self.steps("smart", effort="!MAX", n=1), ["smart/max"])
        # a rerun after one failed run starts a rung up
        self.assertEqual(self.steps("smart", start=1), ["smart/xhigh", "smart/max", "smart/max"])
        self.assertEqual(self.steps("smart", start=1, escalate=False), ["smart/xhigh"] * 3)
        self.assertEqual(self.steps("smart", start=99, n=1), ["smart/max"])
        m = modes.for_attempt("smart", {"smart": {"model": "opus-x"}}, 1, effort="xhigh")
        self.assertEqual((m.model, m.subagents), ("opus-x", False))

    def test_efforts_are_validated(self):
        self.assertEqual(modes.normalize_effort(" XHigh "), "xhigh")
        for bad in ("extra", "", "ultra"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                modes.normalize_effort(bad)
        with self.assertRaises(ValueError):
            modes.resolve("smart", {"smart": {"effort": "huge"}})

    def test_resolve_with_config_override(self):
        m = modes.resolve("fast", {"balanced": {"model": "haiku"}})
        self.assertEqual((m.name, m.model, m.effort, m.subagents), ("balanced", "haiku", "medium", False))
        self.assertTrue(modes.resolve("deep", {}).subagents)

    def test_toolsets(self):
        smart, ultra = modes.resolve("smart", {}), modes.resolve("ultra", {})
        code = modes.tool_args("code", smart)
        self.assertIn("--strict-mcp-config", code)
        self.assertNotIn("Agent", code[1])
        self.assertNotIn("ToolSearch", code[1])
        self.assertIn("Agent", modes.tool_args("code", ultra)[1])
        research = modes.tool_args("research", smart)
        # research keeps MCP, so ToolSearch is mandatory (without it MCP schemas load inline)
        self.assertNotIn("--strict-mcp-config", research)
        self.assertIn("ToolSearch", research[1])

    def test_config_rejects_unknown_default_mode(self):
        with tempfile.TemporaryDirectory() as d:
            os.environ["SADYANX_ROOT"] = d
            try:
                (Path(d) / "config").mkdir()
                (Path(d) / "config/sadyanx.toml").write_text('[defaults]\nmode = "turbo"\n')
                with self.assertRaises(config.ConfigError):
                    config.load()
                (Path(d) / "config/sadyanx.toml").write_text('[defaults]\nmode = "fast"\n[modes.cheap]\nmodel="x"\n')
                self.assertEqual(config.load().mode().name, "balanced")
                for bad in ('[modes.smart]\neffort = "extra"\n', '[defaults]\nescalation_ceiling = "top"\n',
                            '[defaults]\nescalate_last_attempt = true\n'):
                    (Path(d) / "config/sadyanx.toml").write_text(bad)
                    with self.subTest(bad=bad), self.assertRaises(config.ConfigError):
                        config.load()
            finally:
                os.environ.pop("SADYANX_ROOT")


class TestSkills(unittest.TestCase):
    def test_frontmatter_plain_folded_and_none(self):
        plain = "---\nname: lint-rules\ndescription: Use when touching lint config.\n---\nbody"
        folded = ("---\nname: design-system\ndescription: >\n  Enforces the design system\n  for Vue files.\n"
                  "allowed-tools: Read\n---\n# x")
        none = "# AWS Naming Skill\n\n## Purpose\n\nGenerate consistent AWS names\nfor the project.\n\n## More"
        self.assertEqual(skill_meta(plain, "d"), ("lint-rules", "Use when touching lint config."))
        self.assertEqual(skill_meta(folded, "d"), ("design-system", "Enforces the design system for Vue files."))
        self.assertEqual(skill_meta(none, "aws-naming"), ("aws-naming", "Generate consistent AWS names for the project."))

    def test_section(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(skills_section(Path(d)), "")
            s = Path(d) / ".claude/skills/ds"
            s.mkdir(parents=True)
            (s / "SKILL.md").write_text("---\nname: ds\ndescription: Design rules.\n---\n")
            out = skills_section(Path(d))
            self.assertIn("**ds** (`.claude/skills/ds/SKILL.md`): Design rules.", out)


class TestUsage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SADYANX_ROOT"] = self.tmp.name
        t1 = tasks.create("Fast win", "x", type="code", repo="r1", status="ready", mode="smart")
        t1.set(status="done")
        t1.save()
        t2 = tasks.create("Hard one", "x", type="code", repo="r1", status="ready", mode="smart")
        t2.set(status="failed")
        t2.save()
        now = tasks.now()

        def rec(task, phase, attempt, mode, tok):
            return {"ts": now, "task": task, "type": "code", "repo": "r1", "phase": phase, "attempt": attempt,
                    "mode": mode, "model": "m", "effort": "e", "outcome": "done", "turns": 5, "seconds": 10,
                    "cost_usd": 0.5, "tokens": {"input": 10, "output": 90, "cache_read": tok, "cache_write": 0}}

        for tid, recs in (("0001", [rec("0001", "work", 1, "smart", 1000)]),
                          ("0002", [rec("0002", "work", 1, "smart", 2000), rec("0002", "fix", 2, "smart", 3000),
                                    rec("0002", "fix", 3, "ultra", 4000)])):
            d = Path(self.tmp.name) / "runs" / tid
            d.mkdir(parents=True)
            (d / "usage.jsonl").write_text("\n".join(json.dumps(r) for r in recs) + "\n")

    def tearDown(self):
        os.environ.pop("SADYANX_ROOT", None)
        self.tmp.cleanup()

    def test_summary(self):
        s = usage.summarize(30)
        self.assertEqual(s["total"]["calls"], 4)
        self.assertEqual(s["total"]["tokens"], 10000 + 4 * 100)
        self.assertEqual(s["by_phase"]["fix"]["tokens"], 7200)
        self.assertEqual(s["by_mode"]["ultra"]["calls"], 1)
        top = s["tasks"][0]
        self.assertEqual((top["id"], top["first_pass"], top["modes"]), ("0002", False, ["smart", "ultra"]))
        self.assertTrue(s["tasks"][1]["first_pass"])
        day = tasks.now()[:10]
        self.assertEqual(list(s["by_day"]), [day])
        self.assertEqual((s["by_day"][day]["calls"], s["total"]["seconds"]), (4, 40))

    def test_render_and_json(self):
        text = usage.render(30)
        self.assertIn("4 agent calls", text)
        self.assertIn("Hard one", text)
        self.assertEqual(json.loads(usage.as_json(30))["total"]["calls"], 4)

    def test_old_records_are_ignored(self):
        self.assertEqual(usage.render(0), "no agent runs in the last 0 days")


if __name__ == "__main__":
    unittest.main()
