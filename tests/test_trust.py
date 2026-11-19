import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sadyanxlib import trust  # noqa: E402


class TestTrust(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(os.path.realpath(self.tmp.name))
        os.environ["CLAUDE_CONFIG_DIR"] = str(self.home)
        self.cfg = self.home / ".claude.json"
        self.state = self.home / "state"
        (self.state / "worktrees" / "0001").mkdir(parents=True)

    def tearDown(self):
        os.environ.pop("CLAUDE_CONFIG_DIR", None)
        self.tmp.cleanup()

    def test_trusting_the_state_dir_covers_every_workspace_under_it(self):
        self.assertFalse(trust.trusted(self.state / "worktrees" / "0001"))
        self.assertTrue(trust.trust(self.state))
        self.assertTrue(trust.trusted(self.state / "worktrees" / "0001"))
        self.assertTrue(trust.trusted(self.state / "workspaces" / "not-created-yet"))
        self.assertFalse(trust.trusted(self.home))  # not the parent
        self.assertFalse(trust.trust(self.state))  # second time: nothing to do

    def test_a_worktree_is_trusted_only_through_its_main_repo(self):
        repo = self.home / "projects" / "repo"
        repo.mkdir(parents=True)
        wt = self.state / "worktrees" / "0001"
        # a trusted parent (of the worktree or of the repo) does not count for git
        trust.trust(self.state)
        trust.trust(self.home / "projects")
        self.assertFalse(trust.trusted(wt, git_repo=repo))
        trust.trust(repo)
        self.assertTrue(trust.trusted(wt, git_repo=repo))

    def test_only_that_key_changes(self):
        other = {"numStartups": 7, "projects": {"/x": {"allowedTools": ["Bash"], "hasTrustDialogAccepted": False},
                                                str(self.state): {"lastCost": 1.5}}}
        self.cfg.write_text(json.dumps(other))
        os.chmod(self.cfg, 0o600)
        trust.trust(self.state)
        data = json.loads(self.cfg.read_text())
        self.assertEqual(data["numStartups"], 7)
        self.assertEqual(data["projects"]["/x"], other["projects"]["/x"])
        self.assertEqual(data["projects"][str(self.state)], {"lastCost": 1.5, "hasTrustDialogAccepted": True})
        self.assertEqual(self.cfg.stat().st_mode & 0o777, 0o600)
        self.assertEqual([p.name for p in self.home.iterdir() if p.name.startswith(".claude.json.")], [])

    def test_a_broken_config_is_left_alone(self):
        self.cfg.write_text("{not json")
        with self.assertRaises(ValueError):
            trust.trust(self.state)
        self.assertEqual(self.cfg.read_text(), "{not json")
        self.assertFalse(trust.trusted(self.state))

    def test_an_explicitly_untrusted_parent_does_not_count(self):
        self.cfg.write_text(json.dumps({"projects": {str(self.state): {"hasTrustDialogAccepted": False}}}))
        self.assertFalse(trust.trusted(self.state / "worktrees" / "0001"))


if __name__ == "__main__":
    unittest.main()
