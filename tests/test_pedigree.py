import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ValidationError
from src.repository import SQLiteRepository
from src.rules import (
    FOUNDER_OVERLAP_LIMIT,
    build_founder_ledger,
    founder_contributions,
    founder_overlap,
)
from src.rules import RuleEngine
from src.service import DomainService


class PedigreeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.coordinator = Actor("coordinator-1", "coordinator")

    def tearDown(self):
        self.tmp.cleanup()

    def _create_animal(self, animal_id, sex="male", **parents):
        data = {"name": animal_id, "sex": sex}
        data.update(parents)
        return self.service.create(
            self.admin, "animal", dict(data, id=animal_id)
        )

    def _build_tree(self):
        # A1, A2, A3 是没写父本的奠基个体；其余顺着父母链回算。
        self._create_animal("A1", "male")
        self._create_animal("A2", "female")
        self._create_animal("A3", "male")
        self._create_animal("B1", "male", sire_id="A1", dam_id="A2")
        self._create_animal("B2", "female", sire_id="A1", dam_id="A3")
        self._create_animal("C1", "female", sire_id="B1", dam_id="B2")
        return {a["id"]: a for a in self.service.list("animal")}

    def test_founder_contributions_walk_parent_chain(self):
        by_id = self._build_tree()
        self.assertEqual(
            founder_contributions("B1", by_id), {"A1": 0.5, "A2": 0.5}
        )
        self.assertEqual(
            founder_contributions("C1", by_id),
            {"A1": 0.5, "A2": 0.25, "A3": 0.25},
        )

    def test_ledger_shares_across_living_animals(self):
        self._build_tree()
        ledger = self.service.founder_ledger()
        by_founder = {entry["founder_id"]: entry for entry in ledger["founders"]}
        self.assertEqual(ledger["living_count"], 6)
        self.assertEqual(by_founder["A1"]["share"], round(5 / 12, 6))
        self.assertEqual(by_founder["A2"]["share"], round(7 / 24, 6))
        self.assertEqual(by_founder["A3"]["share"], round(7 / 24, 6))
        # 台账按占比降序。
        shares = [entry["share"] for entry in ledger["founders"]]
        self.assertEqual(shares, sorted(shares, reverse=True))
        # 携带个体按贡献降序，且包含非存活个体的占比展示。
        a1_carriers = {c["animal_id"]: c for c in by_founder["A1"]["carriers"]}
        self.assertEqual(a1_carriers["A1"]["contribution"], 1.0)
        self.assertEqual(a1_carriers["C1"]["contribution"], 0.5)
        contributions = [c["contribution"] for c in by_founder["A1"]["carriers"]]
        self.assertEqual(contributions, sorted(contributions, reverse=True))

    def test_quarantined_kept_as_living_but_deceased_excluded(self):
        self._build_tree()
        self.service.transition(
            self.admin, "A2", "mark_deceased", {"cause": "age"}
        )
        self.service.transition(
            self.admin, "B2", "quarantine_animal", {"reason": "exposure"}
        )
        ledger = self.service.founder_ledger()
        by_founder = {entry["founder_id"]: entry for entry in ledger["founders"]}
        # A2 已去世：不再作为存活个体进入分母，但奠基者条目照常显示；
        # 存活后代 B1/C1 携带的 A2 血缘仍计入占比。
        self.assertEqual(ledger["living_count"], 5)
        self.assertEqual(by_founder["A2"]["founder_status"], "deceased")
        self.assertEqual(by_founder["A2"]["share"], 0.15)
        self.assertEqual(
            {c["animal_id"] for c in by_founder["A2"]["carriers"]}, {"B1", "C1"}
        )
        # B2 隔离：仍算存活，贡献照常进入分母。
        b2_status = {
            c["animal_id"]: c["status"]
            for c in by_founder["A3"]["carriers"]
        }
        self.assertEqual(b2_status.get("B2"), "quarantined")

        # 已去世且没有存活后代的奠基者：占比为 0，仍列出台账末尾。
        self._create_animal("D0", "male")
        self.service.transition(
            self.admin, "D0", "mark_deceased", {"cause": "age"}
        )
        ledger = self.service.founder_ledger()
        d0 = next(entry for entry in ledger["founders"] if entry["founder_id"] == "D0")
        self.assertEqual(d0["share"], 0.0)
        self.assertEqual(d0["carrier_count"], 0)
        self.assertEqual(ledger["founders"][-1]["founder_id"], "D0")

    def test_overlap_metric(self):
        by_id = self._build_tree()
        overlap, shared = founder_overlap(by_id["B1"], by_id["B2"], by_id)
        # B1 = 1/2 A1 + 1/2 A2；B2 = 1/2 A1 + 1/2 A3，仅 A1 重合。
        self.assertEqual(overlap, 0.5)
        self.assertEqual(shared[0]["founder_id"], "A1")
        self.assertEqual(shared[0]["overlap"], 0.5)

    def test_pairing_create_blocks_over_limit_and_names_top_founder(self):
        self._build_tree()
        with self.assertRaises(ValidationError) as caught:
            self.service.create(
                self.coordinator,
                "pairing",
                {
                    "proposed_by": "coordinator-1",
                    "sire_id": "B1",
                    "dam_id": "C1",
                },
            )
        message = str(caught.exception)
        self.assertIn(str(FOUNDER_OVERLAP_LIMIT), message)
        self.assertIn("A1", message)
        # 超上限的建议根本没有进入待审（proposed）。
        self.assertEqual(self.service.list("pairing"), [])

    def test_pairing_create_within_limit_records_overlap(self):
        self._build_tree()
        pairing = self.service.create(
            self.coordinator,
            "pairing",
            {"proposed_by": "coordinator-1", "sire_id": "A1", "dam_id": "A2"},
        )
        self.assertEqual(pairing["status"], "proposed")
        self.assertEqual(pairing["data"]["founder_overlap"], 0.0)
        self.assertIsNone(pairing["data"]["top_shared_founder_id"])

    def test_pairing_rejects_non_active_parent(self):
        self._build_tree()
        self.service.transition(
            self.admin, "B2", "quarantine_animal", {"reason": "exposure"}
        )
        with self.assertRaises(ValidationError):
            self.service.create(
                self.coordinator,
                "pairing",
                {"proposed_by": "coordinator-1", "sire_id": "B1", "dam_id": "B2"},
            )

    def test_pairing_preview_is_read_only(self):
        self._build_tree()
        before = len(self.service.list("pairing"))
        preview = self.service.pairing_overlap("B1", "C1")
        self.assertFalse(preview["within_limit"])
        self.assertEqual(preview["top_shared_founder_id"], "A1")
        # 试算不产生任何配对记录。
        self.assertEqual(len(self.service.list("pairing")), before)
        preview = self.service.pairing_overlap("A1", "A2")
        self.assertTrue(preview["within_limit"])
        self.assertEqual(preview["founder_overlap"], 0.0)

    def test_animal_without_sire_is_its_own_founder(self):
        self._create_animal("X1", "female")
        self._create_animal("X2", "male", dam_id="X1")
        by_id = {a["id"]: a for a in self.service.list("animal")}
        # 没写父本即奠基；X2 只写了母本，自身仍按奠基个体算。
        self.assertEqual(founder_contributions("X2", by_id), {"X2": 1.0})
        ledger = build_founder_ledger(list(by_id.values()))
        founder_ids = [entry["founder_id"] for entry in ledger["founders"]]
        self.assertEqual(set(founder_ids), {"X1", "X2"})


if __name__ == "__main__":
    unittest.main()
