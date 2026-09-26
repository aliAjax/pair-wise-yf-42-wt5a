import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ValidationError
from src.repository import SQLiteRepository
from src.rules import FOUNDER_OVERLAP_LIMIT, RuleEngine
from src.service import DomainService


class FounderLedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.vet = Actor("vet", "veterinarian")

    def tearDown(self):
        self.tmp.cleanup()

    def _animal(self, name, sex, sire_id=None, dam_id=None):
        data = {"name": name, "sex": sex}
        if sire_id:
            data["sire_id"] = sire_id
        if dam_id:
            data["dam_id"] = dam_id
        return self.service.create(self.admin, "animal", data)

    def test_ledger_shares_sorted_with_carriers(self):
        f1 = self._animal("F1", "male")
        f2 = self._animal("F2", "female")
        c1 = self._animal("C1", "female", sire_id=f1["id"], dam_id=f2["id"])
        g1 = self._animal("G1", "male", sire_id=c1["id"], dam_id=f2["id"])
        ledger = self.service.founder_ledger()
        self.assertEqual(ledger["animal_total"], 4)
        self.assertEqual(ledger["living_total"], 4)
        founders = ledger["founders"]
        self.assertEqual(
            [entry["founder_id"] for entry in founders], [f2["id"], f1["id"]]
        )
        self.assertAlmostEqual(founders[0]["share"], 0.5625)
        self.assertAlmostEqual(founders[1]["share"], 0.4375)
        carriers = {c["animal_id"]: c["contribution"] for c in founders[1]["carriers"]}
        self.assertAlmostEqual(carriers[f1["id"]], 1.0)
        self.assertAlmostEqual(carriers[c1["id"]], 0.5)
        self.assertAlmostEqual(carriers[g1["id"]], 0.25)
        self.assertNotIn(f2["id"], carriers)

    def test_animal_without_recorded_sire_is_founder(self):
        dam = self._animal("D", "female")
        kid = self._animal("K", "male", dam_id=dam["id"])
        ledger = self.service.founder_ledger()
        founder_ids = [entry["founder_id"] for entry in ledger["founders"]]
        self.assertIn(kid["id"], founder_ids)
        self.assertIn(dam["id"], founder_ids)

    def test_quarantined_and_deceased_still_listed(self):
        f1 = self._animal("F1", "male")
        f2 = self._animal("F2", "female")
        c1 = self._animal("C1", "female", sire_id=f1["id"], dam_id=f2["id"])
        self.service.transition(self.vet, f2["id"], "mark_deceased", {"cause": "age"})
        self.service.transition(
            self.vet, c1["id"], "quarantine_animal", {"reason": "check"}
        )
        ledger = self.service.founder_ledger()
        self.assertEqual(ledger["living_total"], 2)
        founders = {entry["founder_id"]: entry for entry in ledger["founders"]}
        self.assertEqual(founders[f2["id"]]["status"], "deceased")
        # deceased f2 is out of the living pool: share = (0 + 0.5) / 2
        self.assertAlmostEqual(founders[f2["id"]]["share"], 0.25)
        # quarantined c1 is alive and still counts: share = (1 + 0.5) / 2
        self.assertAlmostEqual(founders[f1["id"]]["share"], 0.75)
        carrier_status = {
            c["animal_id"]: c["status"] for c in founders[f2["id"]]["carriers"]
        }
        self.assertEqual(carrier_status[f2["id"]], "deceased")
        self.assertEqual(carrier_status[c1["id"]], "quarantined")

    def test_pairing_over_overlap_limit_rejected_with_top_founder(self):
        f1 = self._animal("F1", "male")
        f2 = self._animal("F2", "female")
        c1 = self._animal("C1", "female", sire_id=f1["id"], dam_id=f2["id"])
        with self.assertRaises(ValidationError) as ctx:
            self.service.create(
                self.admin,
                "pairing",
                {"proposed_by": "coord", "sire_id": f1["id"], "dam_id": c1["id"]},
            )
        message = str(ctx.exception)
        self.assertIn("F1", message)
        self.assertIn(f1["id"], message)

    def test_pairing_at_limit_created_with_overlap_fields(self):
        f1 = self._animal("F1", "male")
        f2 = self._animal("F2", "female")
        f3 = self._animal("F3", "female")
        c1 = self._animal("C1", "female", sire_id=f1["id"], dam_id=f2["id"])
        g1 = self._animal("G1", "male", sire_id=c1["id"], dam_id=f3["id"])
        # g1 carries 0.25 of f2; pairing with f2 overlaps exactly at the limit
        pairing = self.service.create(
            self.admin,
            "pairing",
            {"proposed_by": "coord", "sire_id": g1["id"], "dam_id": f2["id"]},
        )
        self.assertEqual(pairing["status"], "proposed")
        self.assertAlmostEqual(pairing["data"]["founder_overlap"], FOUNDER_OVERLAP_LIMIT)
        self.assertEqual(pairing["data"]["founder_overlap_limit"], FOUNDER_OVERLAP_LIMIT)
        self.assertEqual(pairing["data"]["top_shared_founder"], f2["id"])

    def test_quarantined_or_deceased_cannot_enter_new_pairing(self):
        f1 = self._animal("F1", "male")
        f2 = self._animal("F2", "female")
        f3 = self._animal("F3", "female")
        self.service.transition(
            self.vet, f2["id"], "quarantine_animal", {"reason": "check"}
        )
        self.service.transition(self.vet, f3["id"], "mark_deceased", {"cause": "age"})
        for dam in (f2, f3):
            with self.assertRaises(ValidationError):
                self.service.create(
                    self.admin,
                    "pairing",
                    {"proposed_by": "coord", "sire_id": f1["id"], "dam_id": dam["id"]},
                )


if __name__ == "__main__":
    unittest.main()
