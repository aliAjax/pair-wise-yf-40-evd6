import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class TraceabilityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")

    def tearDown(self):
        self.tmp.cleanup()

    def _consignment(self, code, **extra):
        data = {"code": code, "origin": "Port-A", "destination": "Farm-B"}
        data.update(extra)
        return self.service.create(self.admin, "consignment", data)

    def _inspect_clean(self, entity_id):
        return self.service.transition(
            self.admin, entity_id, "inspect",
            {"inspector": "I-1", "inspection_result": "clean"},
        )

    def _quarantine(self, entity_id):
        self._inspect_clean(entity_id)
        return self.service.transition(
            self.admin, entity_id, "quarantine",
            {"pest_found": True, "sample_id": "S-1"},
        )

    def test_source_must_exist(self):
        with self.assertRaises(ValidationError):
            self._consignment("C-1", source_ids=["no-such-batch"])

    def test_source_registered_on_create(self):
        upstream = self._consignment("C-1")
        downstream = self._consignment("C-2", source_ids=[upstream["id"]])
        self.assertEqual(downstream["data"]["source_ids"], [upstream["id"]])

    def test_self_cycle_rejected(self):
        self._consignment("C-1", id="SELF")
        with self.assertRaises(ValidationError):
            self._consignment("C-2", id="SELF", source_ids=["SELF"])

    def test_transitive_cycle_rejected(self):
        rules = RuleEngine()
        store = {
            "A": {"id": "A", "kind": "consignment", "status": "declared",
                  "data": {"source_ids": ["B"]}},
            "B": {"id": "B", "kind": "consignment", "status": "declared",
                  "data": {"source_ids": []}},
        }

        def lookup(kind, field, value):
            if field == "id":
                return [store[value]] if value in store else []
            return []

        with self.assertRaises(ValidationError):
            rules.validate_create(
                self.admin, "consignment",
                {"code": "C-9", "origin": "O", "destination": "D", "source_ids": ["A"]},
                lookup, entity_id="B",
            )

    def test_recheck_requires_pass_and_records_conclusion(self):
        batch = self._consignment("C-1")
        self._quarantine(batch["id"])
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.admin, batch["id"], "recheck", {"sample_id": "S-2"}
            )
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.admin, batch["id"], "recheck",
                {"sample_id": "S-2", "recheck_result": "fail"},
            )
        self.assertEqual(self.service.get(batch["id"])["status"], "quarantined")
        rechecked = self.service.transition(
            self.admin, batch["id"], "recheck",
            {"sample_id": "S-2", "recheck_result": "pass", "conclusion": "复检合格"},
        )
        self.assertEqual(rechecked["status"], "inspected")
        self.assertEqual(rechecked["data"]["recheck_conclusion"], "复检合格")
        self.assertEqual(rechecked["data"]["rechecked_by"], "admin")
        self.assertFalse(rechecked["data"]["pest_found"])
        released = self.service.transition(
            self.admin, batch["id"], "release",
            {"pest_found": False, "treatment": "completed"},
        )
        self.assertEqual(released["status"], "released")

    def test_upstream_quarantine_blocks_downstream_release(self):
        upstream = self._consignment("C-1", id="UP")
        mid = self._consignment("C-2", id="MID", source_ids=["UP"])
        leaf = self._consignment("C-3", id="LEAF", source_ids=["MID"])
        self._quarantine("UP")
        self._inspect_clean("MID")
        self._inspect_clean("LEAF")
        for target in ("MID", "LEAF"):
            with self.assertRaises(ValidationError):
                self.service.transition(
                    self.admin, target, "release",
                    {"pest_found": False, "treatment": "completed"},
                )
        self.service.transition(
            self.admin, "UP", "recheck",
            {"sample_id": "S-2", "recheck_result": "pass"},
        )
        self.service.transition(
            self.admin, "UP", "release",
            {"pest_found": False, "treatment": "certified"},
        )
        for target in ("MID", "LEAF"):
            released = self.service.transition(
                self.admin, target, "release",
                {"pest_found": False, "treatment": "completed"},
            )
            self.assertEqual(released["status"], "released")

    def test_trace_returns_paths_and_risk(self):
        self._consignment("C-A", id="A", destination="Farm-B")
        self._consignment("C-B", id="B", source_ids=["A"], destination="Farm-B")
        self._consignment("C-C", id="C", source_ids=["B"], destination="Plot-9")
        self._consignment("C-D", id="D", source_ids=["A"], destination="Plot-9")
        self._quarantine("A")
        self._inspect_clean("B")
        facility = self.service.create(
            self.admin, "facility", {"name": "Farm-B", "address": "County 1"}
        )
        traced = self.service.transition(
            self.admin, facility["id"], "trace",
            {"consignment_ids": ["A", "A"]},
        )
        self.assertEqual(traced["status"], "traced")
        result = traced["data"]["trace_result"]
        self.assertEqual(result["starts"], ["A"])
        self.assertEqual(result["paths"], [["A", "B", "C"], ["A", "D"]])
        self.assertEqual(result["batches"]["A"]["risk"], "quarantined")
        self.assertEqual(result["batches"]["B"]["risk"], "inspected")
        self.assertEqual(result["batches"]["C"]["risk"], "declared")
        self.assertEqual(result["batches"]["D"]["risk"], "declared")
        self.assertEqual(
            result["affected_facilities"],
            [{"id": facility["id"], "name": "Farm-B"}],
        )
        retraced = self.service.transition(
            self.admin, facility["id"], "trace", {"consignment_ids": ["A"]}
        )
        self.assertEqual(retraced["status"], "traced")

    def test_trace_unknown_consignment_rejected(self):
        facility = self.service.create(
            self.admin, "facility", {"name": "Farm-B", "address": "County 1"}
        )
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.admin, facility["id"], "trace",
                {"consignment_ids": ["ghost"]},
            )

    def test_rereport_same_code_reuses_original(self):
        first = self._consignment("C-1")
        second = self._consignment("C-1", destination="Elsewhere")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(second["data"]["destination"], "Farm-B")
        items = self.service.list("consignment")
        self.assertEqual(len(items), 1)


if __name__ == "__main__":
    unittest.main()
