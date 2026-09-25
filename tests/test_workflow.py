import tempfile
import unittest
from pathlib import Path

from src.domain import Actor
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


def _resolve(value, created):
    if isinstance(value, str):
        for key, item in created.items():
            value = value.replace("{" + key + "}", str(item))
        return value
    if isinstance(value, list):
        return [_resolve(item, created) for item in value]
    if isinstance(value, dict):
        return {key: _resolve(item, created) for key, item in value.items()}
    return value


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.actor = Actor("admin", "admin")

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_workflow(self):
        created = {}
        steps = [{'op': 'create', 'as': 'consignment', 'kind': 'consignment', 'data': {'code': 'C-1', 'origin': 'Port-A', 'destination': 'Farm-B'}}, {'op': 'transition', 'target': 'consignment', 'action': 'inspect', 'data': {'inspector': 'I-1', 'inspection_result': 'suspected'}, 'expect': 'inspected'}, {'op': 'transition', 'target': 'consignment', 'action': 'quarantine', 'data': {'pest_found': True, 'sample_id': 'S-1'}, 'expect': 'quarantined'}, {'op': 'transition', 'target': 'consignment', 'action': 'destroy', 'data': {'method': 'incineration', 'witnessed_by': 'W-1'}, 'expect': 'destroyed'}, {'op': 'create', 'as': 'facility', 'kind': 'facility', 'data': {'name': 'Farm-B', 'address': 'County 1'}}, {'op': 'transition', 'target': 'facility', 'action': 'trace', 'data': {'consignment_ids': ['{consignment}']}, 'expect': 'traced'}]
        for step in steps:
            if step["op"] == "create":
                entity = self.service.create(
                    self.actor,
                    step["kind"],
                    _resolve(step.get("data", {}), created),
                    step.get("idempotency_key"),
                )
                created[step["as"]] = entity["id"]
            else:
                entity = self.service.transition(
                    self.actor,
                    created[step["target"]],
                    step["action"],
                    _resolve(step.get("data", {}), created),
                    step.get("expected_version"),
                )
            if "expect" in step:
                self.assertEqual(entity["status"], step["expect"])

    def test_trace_report_and_repeated_report(self):
        admin = self.actor
        first = self.service.create(admin, 'consignment', {
            'code': 'C-A', 'origin': 'Port-A', 'destination': 'Farm-B',
        })
        second = self.service.create(admin, 'consignment', {
            'code': 'C-B', 'origin': 'Farm-B', 'destination': 'Farm-C',
            'parent_id': first["id"],
        })
        third = self.service.create(admin, 'consignment', {
            'code': 'C-C', 'origin': 'Farm-C', 'destination': 'Farm-D',
            'parent_id': second["id"],
        })
        fourth = self.service.create(admin, 'consignment', {
            'code': 'C-D', 'origin': 'Farm-C', 'destination': 'Farm-E',
            'parent_id': second["id"],
        })
        self.service.transition(admin, first["id"], 'inspect', {
            'inspector': 'I-1', 'inspection_result': 'suspected',
        })
        self.service.transition(admin, first["id"], 'quarantine', {
            'pest_found': True, 'sample_id': 'S-1',
        })
        facility = self.service.create(admin, 'facility', {
            'name': 'Farm-D', 'address': 'County 9',
        })
        traced = self.service.transition(admin, facility["id"], 'trace', {
            'consignment_ids': [first["id"]],
        })
        report = traced["data"]["trace_report"]
        self.assertEqual(traced["status"], "traced")
        self.assertEqual(
            report["paths"],
            [
                [first["id"], second["id"], third["id"]],
                [first["id"], second["id"], fourth["id"]],
            ],
        )
        risks = {item["id"]: item["risk"] for item in report["batches"]}
        self.assertEqual(risks[first["id"]], "isolated")
        self.assertEqual(risks[second["id"]], "pending")
        self.assertEqual(risks[third["id"]], "pending")
        self.assertEqual(risks[fourth["id"]], "pending")
        self.assertEqual(report["facilities"], [
            {"id": facility["id"], "name": "Farm-D", "status": "registered"},
        ])

        # 上游复检合格并放行后，同一批再次上报仍沿用首次追溯结果
        self.service.transition(admin, first["id"], 'recheck', {
            'sample_id': 'S-2', 'recheck_result': 'passed',
        })
        self.service.transition(admin, first["id"], 'release', {
            'pest_found': False, 'treatment': 'completed',
        })
        retrace = self.service.transition(admin, facility["id"], 'trace', {
            'consignment_ids': [first["id"]],
        })
        second_report = retrace["data"]["trace_report"]
        second_risks = {item["id"]: item["risk"] for item in second_report["batches"]}
        first_entry = next(
            item for item in second_report["batches"] if item["id"] == first["id"]
        )
        self.assertEqual(second_risks[first["id"]], "isolated")
        self.assertEqual(first_entry["status"], "quarantined")
        self.assertEqual(
            [item["id"] for item in second_report["batches"]],
            [item["id"] for item in report["batches"]],
        )


if __name__ == "__main__":
    unittest.main()
