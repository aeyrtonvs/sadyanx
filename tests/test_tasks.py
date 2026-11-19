import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sadyanxlib import tasks  # noqa: E402


class TestTasks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SADYANX_ROOT"] = self.tmp.name

    def tearDown(self):
        os.environ.pop("SADYANX_ROOT", None)
        self.tmp.cleanup()

    def test_roundtrip_and_ids(self):
        body = "## Context\n\nFix #12: total: wrong\n\n## Acceptance criteria\n\n- [ ] npm test passes\n"
        t1 = tasks.create("PROJ-1234: Total rounding", body, type="code", repo="x", jira="PROJ-1234",
                          status="ready", budget_usd=5.0)
        t2 = tasks.create("Something else", "x", type="research", status="draft")
        self.assertEqual((t1.id, t2.id), ("0001", "0002"))
        self.assertTrue(t1.path.name.startswith("0001-proj-1234-total-rounding"))
        again = tasks.parse(t1.path)
        self.assertEqual(again.title, "PROJ-1234: Total rounding")
        self.assertEqual(again.body, body)
        self.assertEqual(again.num("budget_usd"), 5.0)
        again.set(status="running", cost_usd=1.23456)
        again.save()
        self.assertEqual(tasks.parse(t1.path).get("cost_usd"), "1.2346")

    def test_find(self):
        t = tasks.create("Something", "x", jira="PROJ-9", status="ready")
        for ref in ("1", "0001", t.path.stem, "proj-9", str(t.path)):
            self.assertEqual(tasks.find(ref).id, "0001", ref)
        with self.assertRaises(LookupError):
            tasks.find("99")

    def test_slugify(self):
        self.assertEqual(tasks.slugify("Corrección de Año: ¿ñandú?"), "correccion-de-ano-nandu")


if __name__ == "__main__":
    unittest.main()
