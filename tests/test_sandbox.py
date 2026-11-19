import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sadyanxlib import config, guard, runner, sandbox, tasks  # noqa: E402
from sadyanxlib.guard import DENY  # noqa: E402


def cfg_with(sandbox_opts: dict | None = None) -> config.Config:
    return config.Config(defaults=dict(config.DEFAULTS), notify={"macos": False}, guard={}, repos={},
                         sandbox=sandbox_opts or {})


def repo(**kw) -> config.Repo:
    return config.Repo(name="r", path=Path("/nowhere"), kind="personal", base="main", **kw)


class TestSandboxSettings(unittest.TestCase):
    def test_off_by_default_and_repo_override(self):
        self.assertFalse(sandbox.enabled(cfg_with(), repo()))
        self.assertTrue(sandbox.enabled(cfg_with(), repo(sandbox=True)))
        self.assertFalse(sandbox.enabled(cfg_with({"enabled": True}), repo(sandbox=False)))
        self.assertTrue(sandbox.enabled(cfg_with({"enabled": True}), None))  # research tasks
        self.assertIsNone(sandbox.settings(cfg_with(), repo(), Path("/x")))

    def test_block_closes_the_escape_hatches(self):
        box = sandbox.settings(cfg_with(), repo(sandbox=True), Path(tempfile.mkdtemp()))
        self.assertTrue(box["enabled"])
        self.assertTrue(box["failIfUnavailable"])
        self.assertFalse(box["allowUnsandboxedCommands"])
        self.assertTrue(box["network"]["strictAllowlist"])
        self.assertNotIn("excludedCommands", box)
        self.assertIn(os.path.realpath(os.path.expanduser("~/.ssh")), box["filesystem"]["denyRead"])
        self.assertIn("/tmp", box["filesystem"]["allowWrite"])
        self.assertIn(str(sandbox.cache_root()), box["filesystem"]["allowWrite"])

    def test_repo_extras_and_worktree_git_dir(self):
        tmp = Path(tempfile.mkdtemp())
        main, wt = tmp / "main", tmp / "wt"
        main.mkdir()
        run = lambda *a, cwd: subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *a],  # noqa: E731
                                             cwd=cwd, check=True, capture_output=True)
        run("init", "-q", "-b", "main", cwd=main)
        (main / "f").write_text("x")
        run("add", "-A", cwd=main)
        run("commit", "-qm", "i", cwd=main)
        run("worktree", "add", "-q", "-b", "t", str(wt), cwd=main)
        r = repo(sandbox=True, sandbox_domains=["x.codeartifact.aws"], sandbox_allow_write=["~/extra"])
        box = sandbox.settings(cfg_with({"allowed_domains": ["a.com", "x.codeartifact.aws"]}), r, wt)
        self.assertEqual(box["network"]["allowedDomains"], ["a.com", "x.codeartifact.aws"])  # no duplicates
        writes = box["filesystem"]["allowWrite"]
        self.assertIn(os.path.realpath(main / ".git"), writes)
        self.assertIn(os.path.realpath(os.path.expanduser("~/extra")), writes)

    def test_cache_env_points_into_the_temp_dir(self):
        env = sandbox.cache_env()
        self.assertTrue(all(v.startswith(str(sandbox.cache_root())) for v in env.values()))
        self.assertIn("npm_config_cache", env)


class TestConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SADYANX_ROOT"] = self.tmp.name
        (Path(self.tmp.name) / "config").mkdir()

    def tearDown(self):
        os.environ.pop("SADYANX_ROOT", None)
        self.tmp.cleanup()

    def write(self, harness: str = "", repos: str = "") -> None:
        (Path(self.tmp.name) / "config" / "sadyanx.toml").write_text(harness)
        (Path(self.tmp.name) / "config" / "repos.toml").write_text(repos)

    def test_parses_repo_sandbox_fields(self):
        self.write("[sandbox]\nenabled = false\n",
                   '[repos.a]\npath = "/x"\nbase = "main"\nsandbox = true\nsandbox_domains = ["d.com"]\n')
        cfg = config.load()
        self.assertTrue(cfg.repos["a"].sandbox)
        self.assertEqual(cfg.repos["a"].sandbox_domains, ["d.com"])

    def test_rejects_typos(self):
        self.write("[sandbox]\nenable = true\n")
        with self.assertRaises(config.ConfigError):
            config.load()
        self.write("", '[repos.a]\npath = "/x"\nbase = "main"\nsandbox = "yes"\n')
        with self.assertRaises(config.ConfigError):
            config.load()


class TestGuardAndRunner(unittest.TestCase):
    def test_guard_denies_the_unsandboxed_retry(self):
        c = guard.Ctx(task_id="1", run_dir=Path(tempfile.mkdtemp()), write_roots=["/wt"])
        v = guard.decide("Bash", {"command": "ls", "dangerouslyDisableSandbox": True}, "/wt", c)
        self.assertEqual((v.kind, v.category), (DENY, "sandbox"))
        self.assertEqual(guard.decide("Bash", {"command": "ls", "dangerouslyDisableSandbox": False}, "/wt", c).kind,
                         "allow")

    def test_runner_settings_carry_hooks_and_sandbox(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        os.environ["SADYANX_ROOT"] = str(Path(tmp.name) / "root")
        os.environ["SADYANX_STATE"] = str(Path(tmp.name) / "state")
        self.addCleanup(os.environ.pop, "SADYANX_ROOT", None)
        self.addCleanup(os.environ.pop, "SADYANX_STATE", None)
        task = tasks.create("Research", "x", type="research", status="ready")
        for opts, expect in (({"enabled": True}, True), ({}, False)):
            r = runner.Runner(task, cfg_with(opts))
            r.workdir.mkdir(parents=True, exist_ok=True)
            r.write_guard_files()
            settings = json.loads((r.run_dir / "settings.json").read_text())
            self.assertIn("PreToolUse", settings["hooks"])
            self.assertEqual("sandbox" in settings, expect)


if __name__ == "__main__":
    unittest.main()
