import copy
import hashlib
import io
import json
import tempfile
import tarfile
import uuid
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import zstandard
from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.test import TestCase, TransactionTestCase, RequestFactory, override_settings
from django.utils import timezone

from OpenBench import training_api
from OpenBench.models import EngineConfig, Profile, TrainingRun, TrainingWorker, TrainingWorkload, TrainingCheckpoint, TrainingArtifact, Machine
from OpenBench.schedule_builder import DEFAULT_SPEC, generate_schedule
from OpenBench.training_checkpoints import checkpoint_ready, prune_checkpoints
from OpenBench.training_workloads import expire_workload, refine_candidates


class WorkloadFixture:
    def setUp(self):
        self.storage = tempfile.TemporaryDirectory()
        self.addCleanup(self.storage.cleanup)
        settings = override_settings(TRAINING_ROOT=self.storage.name, TRAINING_REPLICA_ROOT='', TRAINING_STORAGE_RESERVE_BYTES=0)
        settings.enable()
        self.addCleanup(settings.disable)
        self.factory = RequestFactory()
        self.user = User.objects.create_user(username='workloads')
        Profile.objects.create(user=self.user, enabled=True)
        self.engine = EngineConfig.objects.create(name='Example', enabled=True)
        self.secret = 'test-secret'
        self.worker = self.make_worker()
        spec = {**copy.deepcopy(DEFAULT_SPEC), 'superbatches': 5, 'lr_stages': [{'start': 1, 'end': 5, 'kind': 'cosine', 'initial': 0.01, 'final': 0.001}], 'wdl_stages': [{'start': 1, 'end': 5, 'kind': 'linear', 'initial': 0, 'final': 1}]}
        _, files, config = generate_schedule(spec)
        config.update(network_min_bytes=1, network_max_bytes=1000000, checkpoint_keep_last=1)
        self.snapshot = {'files': files, 'settings': config, 'bullet_commit': config['bullet_ref']}
        self.dataset = {'repo': 'test/data', 'commit': 'a' * 40, 'size': 1, 'files': [{'path': 'data.vf', 'size': 1}]}
        self.run = self.make_run()
        self.token = uuid.uuid4()

    def make_worker(self):
        return TrainingWorker.objects.create(owner=self.user, name='Worker', secret_hash=hashlib.sha256(self.secret.encode()).hexdigest(), info={'protocol': 4, 'capabilities': ['training-workloads', 'typed-assignments'], 'backend': 'cuda', 'disk_gb': 1000, 'runtime': {}, 'threads': 2})

    def make_run(self, **kwargs):
        return TrainingRun.objects.create(owner=self.user, engine=self.engine, name='test', snapshot=copy.deepcopy(self.snapshot), dataset=copy.deepcopy(self.dataset), state='QUEUED', workload_size=2, **kwargs)

    def request(self, view, data=None, worker=None, token=None, pk=None, body=None, query=''):
        worker = worker or self.worker
        headers = {'HTTP_AUTHORIZATION': 'Bearer ' + self.secret, 'HTTP_X_TRAINING_WORKER': str(worker.pk), 'HTTP_X_TRAINING_CLAIM': str(token or self.token)}
        request = self.factory.post('/' + query, data=json.dumps(data or {}) if body is None else body, content_type='application/json' if body is None else 'application/octet-stream', **headers)
        response = view(request, *([pk] if pk is not None else []))
        return response.status_code, json.loads(response.content)

    def claim(self, worker=None, token=None):
        status, payload = self.request(training_api.claim, {'claim_id': str(token or self.token), 'disk_gb': 1000}, worker=worker, token=token)
        self.assertEqual(status, 200, payload)
        return payload

    def artifact(self, name, kind, content):
        digest = hashlib.sha256(content).hexdigest()
        status, result = self.request(training_api.upload_artifact, pk=self.run.pk, query='?name=%s&kind=%s&sha256=%s' % (name, kind, digest), body=content)
        self.assertEqual(status, 200, result)
        return result['id']

    def checkpoint(self, sb):
        self.run.refresh_from_db()
        if self.run.state != 'SAVING':
            TrainingRun.objects.filter(pk=self.run.pk).update(state='TRAINING')
        network = b'example-network'
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode='w') as tar:
            for name, data in [('optimiser_state/weights.bin', b'weights'), ('optimiser_state/momentum.bin', b'momentum'), ('optimiser_state/velocity.bin', b'velocity'), ('quantised.bin', network)]:
                member = tarfile.TarInfo(name); member.size = len(data)
                tar.addfile(member, io.BytesIO(data))
        archive_id = self.artifact('checkpoint-%d.tar.zst' % sb, 'checkpoint', zstandard.ZstdCompressor().compress(stream.getvalue()))
        network_id = self.artifact('sb-%d.bin' % sb, 'network', network)
        status, result = self.request(checkpoint_ready, {'superbatch': sb, 'archive': archive_id, 'network': network_id}, pk=self.run.pk)
        self.assertEqual(status, 200, result)
        return TrainingCheckpoint.objects.get(pk=result['checkpoint'])

    def complete(self, sequence):
        TrainingRun.objects.filter(pk=self.run.pk).update(state='SAVING')
        self.artifact('manifest.json', 'manifest', b'{}')
        self.artifact('worker.log', 'log', b'complete log')
        return self.request(training_api.report, {'state': 'COMPLETED', 'sequence': sequence}, pk=self.run.pk)

class WorkloadTests(WorkloadFixture, TestCase):
    def test_claim_complete_rotate_resume_and_retry(self):
        job = self.claim()['run']
        self.assertEqual((job['workload']['start'], job['workload']['end']), (1, 2))
        self.assertEqual(self.claim()['run']['workload'], job['workload'])
        self.assertIsNone(self.claim(self.make_worker(), uuid.uuid4())['run'])
        checkpoint = self.checkpoint(2)
        status, response = self.complete(1)
        self.assertEqual(status, 200, response)
        self.run.refresh_from_db()
        self.assertEqual((self.run.state, self.run.worker_id, self.run.completed_superbatches), ('QUEUED', None, 2))
        self.assertEqual(self.request(training_api.report, {'state': 'COMPLETED', 'sequence': 1}, pk=self.run.pk)[0], 200)
        old_token = self.token
        self.token = uuid.uuid4()
        job = self.claim()['run']
        self.assertEqual((job['workload']['start'], job['workload']['end']), (3, 4))
        self.assertEqual(job['snapshot']['resume']['checkpoint_id'], checkpoint.pk)
        status, result = self.request(training_api.report, {'state': 'TRAINING', 'sequence': 2}, token=old_token, pk=self.run.pk)
        self.assertEqual(status, 400, result)
        self.checkpoint(4)
        self.assertEqual(self.complete(2)[0], 200)
        self.token = uuid.uuid4()
        self.assertEqual(self.claim()['run']['workload']['end'], 5)
        self.checkpoint(5)
        self.assertEqual(self.complete(3)[0], 200)
        self.run.refresh_from_db()
        self.assertEqual(self.run.state, 'COMPLETED')
        self.assertEqual(self.run.workloads.filter(state='COMPLETED').count(), 3)
        self.assertEqual(self.run.metrics['progress'], 100)

    def test_completion_requires_boundary_and_attempt_artifacts(self):
        self.claim()
        self.checkpoint(1)
        status, result = self.complete(1)
        self.assertEqual(status, 400, result)
        self.run.refresh_from_db()
        self.assertEqual(self.run.state, 'SAVING')
        self.assertTrue(self.run.workloads.filter(state='ACTIVE').exists())
        # The continuation checkpoint is protected even with aggressive retention.
        self.checkpoint(2)
        self.assertEqual(self.complete(1)[0], 200)
        self.run.refresh_from_db()
        prune_checkpoints(self.run)
        self.assertTrue(TrainingCheckpoint.objects.filter(pk=self.run.continuation_checkpoint_id).exists())

    def test_expiry_fences_uploads_and_resumes_only_saved_work(self):
        self.claim()
        checkpoint = self.checkpoint(1)
        TrainingRun.objects.filter(pk=self.run.pk).update(metrics={'superbatch': 2})
        expire_workload(self.run)
        old = self.token
        self.token = uuid.uuid4()
        job = self.claim()['run']
        self.assertEqual(job['workload']['start'], 2)
        self.assertEqual(job['snapshot']['resume']['checkpoint_id'], checkpoint.pk)
        status, _ = self.request(training_api.upload_artifact, pk=self.run.pk, token=old, query='?name=stale.log&kind=log&sha256=' + hashlib.sha256(b'x').hexdigest(), body=b'x')
        self.assertEqual(status, 400)
        status, _ = self.request(checkpoint_ready, {'superbatch': 2, 'archive': checkpoint.archive_id, 'network': checkpoint.network_id}, pk=self.run.pk, token=old)
        self.assertEqual(status, 400)
        self.assertEqual(self.run.workloads.filter(state='EXPIRED').count(), 1)

    def test_priority_legacy_worker_and_cancellation(self):
        high = self.make_run(priority=10)
        self.assertEqual(self.claim()['run']['id'], high.pk)
        worker = self.make_worker()
        worker.info['protocol'] = 3; worker.save()
        self.assertIsNone(self.claim(worker, uuid.uuid4())['run'])
        self.run.workload_size = 0; self.run.save()
        self.assertEqual(self.claim(worker, uuid.uuid4())['run']['id'], self.run.pk)
        TrainingRun.objects.filter(pk=high.pk).update(cancel_requested=True)
        expire_workload(high)
        high.refresh_from_db()
        self.assertEqual(high.state, 'CANCELLED')

    def test_constraints_and_no_expiry_after_recent_report(self):
        self.claim()
        workload = self.run.workloads.get()
        with self.assertRaises(IntegrityError), transaction.atomic():
            TrainingWorkload.objects.create(run=self.run, worker=self.make_worker(), claim_token=uuid.uuid4(), start=1, end=2)
        expire_workload(self.run, cutoff=timezone.now() - timedelta(seconds=600))
        workload.refresh_from_db()
        self.assertEqual(workload.state, 'ACTIVE')

    def test_shared_priority_force_and_focus(self):
        train = SimpleNamespace(priority=0, engine=SimpleNamespace(name='A'))
        test = SimpleNamespace(priority=10, dev_engine='B')
        self.assertEqual(refine_candidates([train], [test], {})[:2], ([], [test]))
        self.assertEqual(refine_candidates([train], [test], {'force': ['A']})[:2], ([train], []))
        test.priority = 0
        self.assertEqual(refine_candidates([train], [test], {'focus': ['B']})[:2], ([], [test]))

    def test_typed_tests_share_priority_and_replay_assignment(self):
        machine = Machine.objects.create(user=self.user, info={'concurrency': 2}, mode='automatic')
        self.worker.machine = machine; self.worker.save()
        test = SimpleNamespace(pk=42, priority=5, dev_engine=self.engine.name)
        result = {'workload': {'test': {'id': 42}}}
        with patch('OpenBench.workloads.get_workload.filter_valid_workloads', return_value=([test], False)), patch('OpenBench.workloads.get_workload.get_workload', return_value=result) as allocate:
            response = self.claim()
            self.assertEqual(response['assignment']['type'], 'test')
            self.assertEqual(self.claim(), response)
            self.assertEqual(allocate.call_count, 1)
            self.run.priority = 5; self.run.save()
            self.token = uuid.uuid4()
            response = self.claim()
            self.assertEqual(response['assignment']['type'], 'training')
            self.assertEqual(response['run']['id'], self.run.pk)

    def test_mid_upload_expiry_cannot_commit(self):
        self.claim()
        TrainingRun.objects.filter(pk=self.run.pk).update(state='TRAINING')
        data = b'network'
        headers = {'HTTP_AUTHORIZATION': 'Bearer ' + self.secret, 'HTTP_X_TRAINING_WORKER': str(self.worker.pk), 'HTTP_X_TRAINING_CLAIM': str(self.token)}
        request = self.factory.post('/?name=network.bin&kind=network&sha256=' + hashlib.sha256(data).hexdigest(), data=data, content_type='application/octet-stream', **headers)
        read = request.read
        def expire_then_read(size):
            expire_workload(self.run)
            request.read = read
            return read(size)
        request.read = expire_then_read
        response = training_api.upload_artifact(request, self.run.pk)
        self.assertEqual(response.status_code, 400)
        self.assertFalse(self.run.artifacts.exists())

    def test_final_checkpoint_crash_requires_only_finalization(self):
        self.run.workload_size = 5; self.run.save()
        self.claim()
        self.checkpoint(5)
        expire_workload(self.run)
        self.token = uuid.uuid4()
        job = self.claim()['run']
        self.assertTrue(job['workload']['finalize_only'])
        self.assertEqual(job['snapshot']['resume']['superbatch'], 5)
        self.assertEqual(self.complete(1)[0], 200)
        self.run.refresh_from_db()
        self.assertEqual(self.run.state, 'COMPLETED')


class ConcurrentWorkloadTests(WorkloadFixture, TransactionTestCase):
    def test_concurrent_claims(self):
        from concurrent.futures import ThreadPoolExecutor
        from django.db import connection, close_old_connections
        import threading
        if connection.vendor != 'postgresql':
            self.skipTest('Row-lock concurrency is verified with PostgreSQL.')
        other = self.make_worker()
        barrier = threading.Barrier(2)
        def claim(worker):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return self.claim(worker, uuid.uuid4())
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(claim, [self.worker, other]))
        self.assertEqual(sum(result['run'] is not None for result in results), 1)
        self.assertEqual(TrainingWorkload.objects.filter(state='ACTIVE').count(), 1)

    def test_concurrent_completion_is_idempotent(self):
        from concurrent.futures import ThreadPoolExecutor
        from django.db import connection, close_old_connections
        import threading
        if connection.vendor != 'postgresql':
            self.skipTest('Row-lock concurrency is verified with PostgreSQL.')
        self.claim()
        self.checkpoint(2)
        TrainingRun.objects.filter(pk=self.run.pk).update(state='SAVING')
        self.artifact('manifest.json', 'manifest', b'{}')
        self.artifact('worker.log', 'log', b'complete log')
        barrier = threading.Barrier(2)
        def complete(_):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return self.request(training_api.report, {'state': 'COMPLETED', 'sequence': 1}, pk=self.run.pk)
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(complete, [0, 1]))
        self.assertEqual([result[0] for result in results], [200, 200], results)
        self.assertEqual(self.run.workloads.filter(state='COMPLETED').count(), 1)
