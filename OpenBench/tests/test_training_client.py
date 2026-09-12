import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase
from Client import training_cache
from Client.training_worker import cleanup_runs, WorkerSession


class ClientWorkloadTests(SimpleTestCase):
    def test_cache_roundtrip_checks_content_and_ignores_active_entries(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            active = root / 'attempt'; active.mkdir()
            file = active / 'data.vf'; file.write_bytes(b'checked data')
            cache = root / 'cache' / 'datasets'
            key = training_cache.key({'source': 'pinned', 'shuffle': True})
            self.assertTrue(training_cache.put(cache, key, active, [file], {'statistics': {'games': 1}}))
            self.assertGreater(training_cache.reclaimable_bytes(root / 'cache'), 0)
            restored = training_cache.take(cache, key, active)
            self.assertEqual(restored['metadata']['statistics']['games'], 1)
            self.assertEqual(file.read_bytes(), b'checked data')
            self.assertEqual(training_cache.reclaimable_bytes(root / 'cache'), 0)
            training_cache.put(cache, key, active, [file])
            (cache / key / 'data.vf').write_bytes(b'corrupt data')
            self.assertIsNone(training_cache.take(cache, key, active))
            self.assertFalse((cache / key).exists())

    def test_cleanup_keeps_inactive_caches(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cached = root / 'cache' / 'datasets' / 'key'; cached.mkdir(parents=True)
            (cached / 'data.vf').write_bytes(b'data')
            (root / '123').mkdir()
            cleanup_runs(root)
            self.assertTrue(cached.exists())
            self.assertFalse((root / '123').exists())

    def test_cumulative_report_sequence_does_not_skip_new_workload(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            job = {'id': 1, 'state': 'DOWNLOADING', 'report_sequence': 25, 'workload': {'report_sequence': 0}, 'cancel_requested': False}
            connection = Mock()
            connection.request.return_value.json.return_value = {'run': job}
            registration = {'claim_id': 'old'}
            args = SimpleNamespace(directory=root, unified=True, pawnocchio=root / 'tool')
            session = WorkerSession(args, connection, registration, root / 'identity.json', Mock())
            with patch('Client.training_worker.execute', return_value=False) as execute:
                self.assertTrue(session.poll())
                execute.assert_called_once()
            self.assertNotEqual(registration['claim_id'], 'old')

    def test_typed_test_assignment_advances_claim_and_preserves_settings(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            connection = Mock()
            connection.request.return_value.json.return_value = {'run': None, 'assignment': {'type': 'test', 'workload': {'test': {'id': 7}}}, 'settings': {'focus': ['A']}}
            registration = {'claim_id': 'old'}
            session = WorkerSession(SimpleNamespace(directory=root, unified=True), connection, registration, root / 'identity.json', Mock())
            self.assertFalse(session.poll())
            self.assertEqual(session.pending_test['test']['id'], 7)
            self.assertEqual(session.settings['focus'], ['A'])
            self.assertNotEqual(registration['claim_id'], 'old')
