import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, PermissionDenied, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


def _consignment(service, actor, code, parent_id=None):
    data = {'code': code, 'origin': 'A-' + code, 'destination': 'B-' + code}
    if parent_id:
        data['parent_id'] = parent_id
    return service.create(actor, 'consignment', data)


class FailureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")

    def tearDown(self):
        self.tmp.cleanup()

    def test_permission_denied(self):
        entity = self.service.create(
            self.admin, 'consignment', {'code': 'C-9', 'origin': 'A', 'destination': 'B'}
        )
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                Actor("viewer", "viewer"),
                entity["id"],
                'inspect',
                {'inspector': 'I-1', 'inspection_result': 'clean'},
            )

    def test_version_conflict(self):
        entity = self.service.create(
            self.admin, 'consignment', {'code': 'C-9', 'origin': 'A', 'destination': 'B'}
        )
        with self.assertRaises(ConflictError):
            self.service.transition(
                self.admin,
                entity["id"],
                'inspect',
                {'inspector': 'I-1', 'inspection_result': 'clean'},
                expected_version=999,
            )

    def test_duplicate_idempotency_key_returns_same_entity(self):
        first = self.service.create(
            self.admin,
            'consignment',
            {'code': 'C-9', 'origin': 'A', 'destination': 'B'},
            idempotency_key="duplicate-check",
        )
        second = self.service.create(
            self.admin,
            'consignment',
            {'code': 'C-9', 'origin': 'A', 'destination': 'B'},
            idempotency_key="duplicate-check",
        )
        self.assertEqual(first["id"], second["id"])

    def test_parent_consignment_must_exist(self):
        with self.assertRaises(ValidationError):
            _consignment(self.service, self.admin, 'C-1', parent_id='missing')

    def test_parent_chain_cycle_rejected(self):
        first = _consignment(self.service, self.admin, 'C-1')
        second = _consignment(self.service, self.admin, 'C-2', parent_id=first["id"])
        with self.assertRaises(ValidationError):
            self.service.create(
                self.admin,
                'consignment',
                {
                    'id': first["id"],
                    'code': 'C-3',
                    'origin': 'A',
                    'destination': 'B',
                    'parent_id': second["id"],
                },
            )

    def test_self_parent_rejected(self):
        with self.assertRaises(ValidationError):
            self.service.create(
                self.admin,
                'consignment',
                {
                    'id': 'self-loop',
                    'code': 'C-4',
                    'origin': 'A',
                    'destination': 'B',
                    'parent_id': 'self-loop',
                },
            )

    def test_release_blocked_while_upstream_quarantined(self):
        first = _consignment(self.service, self.admin, 'C-1')
        second = _consignment(self.service, self.admin, 'C-2', parent_id=first["id"])
        third = _consignment(self.service, self.admin, 'C-3', parent_id=second["id"])
        self.service.transition(
            self.admin, first["id"], 'inspect',
            {'inspector': 'I-1', 'inspection_result': 'suspected'},
        )
        self.service.transition(
            self.admin, first["id"], 'quarantine',
            {'pest_found': True, 'sample_id': 'S-1'},
        )
        self.service.transition(
            self.admin, second["id"], 'inspect',
            {'inspector': 'I-1', 'inspection_result': 'clean'},
        )
        self.service.transition(
            self.admin, third["id"], 'inspect',
            {'inspector': 'I-1', 'inspection_result': 'clean'},
        )
        # 直接上游隔离，不能放行
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.admin, second["id"], 'release',
                {'pest_found': False, 'treatment': 'completed'},
            )
        # 隔一代同样受上游隔离影响
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.admin, third["id"], 'release',
                {'pest_found': False, 'treatment': 'completed'},
            )
        # 上游复检合格并放行后，下游才能放行
        self.service.transition(
            self.admin, first["id"], 'recheck',
            {'sample_id': 'S-2', 'recheck_result': 'passed'},
        )
        self.service.transition(
            self.admin, first["id"], 'release',
            {'pest_found': False, 'treatment': 'completed'},
        )
        self.service.transition(
            self.admin, second["id"], 'release',
            {'pest_found': False, 'treatment': 'completed'},
        )
        released = self.service.transition(
            self.admin, third["id"], 'release',
            {'pest_found': False, 'treatment': 'completed'},
        )
        self.assertEqual(released["status"], "released")

    def test_failed_recheck_keeps_consignment_quarantined(self):
        entity = _consignment(self.service, self.admin, 'C-1')
        self.service.transition(
            self.admin, entity["id"], 'inspect',
            {'inspector': 'I-1', 'inspection_result': 'suspected'},
        )
        self.service.transition(
            self.admin, entity["id"], 'quarantine',
            {'pest_found': True, 'sample_id': 'S-1'},
        )
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.admin, entity["id"], 'recheck',
                {'sample_id': 'S-2', 'recheck_result': 'failed'},
            )
        self.assertEqual(self.service.get(entity["id"])["status"], "quarantined")

    def test_trace_unknown_consignment_rejected(self):
        facility = self.service.create(
            self.admin, 'facility', {'name': 'Farm-X', 'address': 'County 1'}
        )
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.admin, facility["id"], 'trace',
                {'consignment_ids': ['nope']},
            )


if __name__ == "__main__":
    unittest.main()
