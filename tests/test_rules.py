import unittest

from src.rules import consignment_risk, trace_downstream, trace_paths
from src.domain import Actor, PermissionDenied, ValidationError
from src.rules import RuleEngine


class RulesTest(unittest.TestCase):
    def setUp(self):
        self.rules = RuleEngine()
        self.admin = Actor("rule-tester", "admin")

    def test_rule_calculation_or_validation(self):
        links = [
            {"id": "1", "parent_id": None},
            {"id": "2", "parent_id": "1"},
            {"id": "3", "parent_id": "2"},
        ]
        self.assertEqual(trace_downstream(links, "1"), ["1", "2", "3"])
        with self.assertRaises(ValidationError):
            self.rules.validate_transition(self.admin, {"kind": "consignment", "status": "inspected", "data": {}}, "release", {"pest_found": True, "treatment": "completed"})

    def test_trace_paths_covers_every_branch(self):
        links = [
            {"id": "1", "parent_id": None},
            {"id": "2", "parent_id": "1"},
            {"id": "3", "parent_id": "2"},
            {"id": "4", "parent_id": "2"},
        ]
        self.assertEqual(
            trace_paths(links, "1"),
            [["1", "2", "3"], ["1", "2", "4"]],
        )

    def test_consignment_risk(self):
        self.assertEqual(consignment_risk({"status": "quarantined", "data": {}}), "isolated")
        self.assertEqual(consignment_risk({"status": "destroyed", "data": {}}), "eliminated")
        self.assertEqual(consignment_risk({"status": "released", "data": {}}), "cleared")
        self.assertEqual(
            consignment_risk({"status": "inspected", "data": {"pest_found": True}}),
            "infected",
        )
        self.assertEqual(consignment_risk({"status": "inspected", "data": {}}), "observing")
        self.assertEqual(consignment_risk({"status": "declared", "data": {}}), "pending")

    def test_recheck_requires_passed_conclusion(self):
        entity = {"kind": "consignment", "status": "quarantined", "data": {}}
        with self.assertRaises(ValidationError):
            self.rules.validate_transition(
                self.admin, entity, "recheck", {"sample_id": "S-2"}
            )
        with self.assertRaises(ValidationError):
            self.rules.validate_transition(
                self.admin, entity, "recheck",
                {"sample_id": "S-2", "recheck_result": "failed"},
            )
        next_status, patch = self.rules.validate_transition(
            self.admin, entity, "recheck",
            {"sample_id": "S-2", "recheck_result": "passed"},
        )
        self.assertEqual(next_status, "inspected")
        self.assertEqual(patch["recheck_result"], "passed")


if __name__ == "__main__":
    unittest.main()
