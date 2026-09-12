"""Opt-in integration test using the compiled fixture from Scripts/prepare_builder_gpu.py.

Set MATTBENCH_TEST_GPU_BINARY, MATTBENCH_TEST_GPU_SPEC and MATTBENCH_TEST_GPU_DATA.
The test database and temporary artifact storage are isolated from deployed services.
"""
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import uuid
from unittest import skipUnless

import zstandard
from django.test import TransactionTestCase
from OpenBench import training_api
from OpenBench.models import TrainingRun, TrainingCheckpoint
from OpenBench.schedule_builder import dataset_stages, generate_schedule
from OpenBench.training_checkpoints import checkpoint_ready
from OpenBench.tests.test_training_workloads import WorkloadFixture


@skipUnless(os.environ.get('MATTBENCH_TEST_GPU_BINARY'), 'Set GPU integration fixture paths to run this test.')
class GPUContinuationTests(WorkloadFixture, TransactionTestCase):
    def test_train_yield_resume_finish(self):
        spec = json.loads(Path(os.environ['MATTBENCH_TEST_GPU_SPEC']).read_text())
        _, files, config = generate_schedule(spec)
        self.assertEqual(dataset_stages(spec), [{'start': 1, 'end': 3}, {'start': 4, 'end': 5}])
        config.update(network_min_bytes=1, checkpoint_keep_last=1)
        self.snapshot = {'files': files, 'settings': config, 'bullet_commit': config['bullet_ref']}
        self.run.snapshot = self.snapshot; self.run.save()
        self.worker.info['backend'] = config['backend']; self.worker.save()
        other = self.make_run()
        first = self.run
        all_metrics = []
        sequence = 0
        allocations = []
        for expected_run in (first.pk, other.pk, first.pk, other.pk, first.pk):
            job = self.claim()['run']
            self.assertEqual(job['id'], expected_run)
            allocations.append((job['id'], job['workload']['start'], job['workload']['end']))
            self.run = TrainingRun.objects.get(pk=job['id'])
            output = Path(self.storage.name) / ('gpu-%s-%s' % (job['id'], self.token.hex))
            output.mkdir()
            listing = output / 'files.txt'; listing.write_text(os.environ['MATTBENCH_TEST_GPU_DATA'] + '\n')
            env = dict(os.environ)
            env.update(MATTBENCH_DATA_FILES=str(listing), MATTBENCH_NET_NAME='test', MATTBENCH_OUTPUT_DIR=str(output), MATTBENCH_START_SUPERBATCH=str(job['workload']['start']), MATTBENCH_END_SUPERBATCH=str(job['workload']['end']))
            for i in range(3): env['MATTBENCH_STAGE_%d_FILES' % i] = str(listing)
            if 'resume' in job['snapshot']:
                checkpoint = TrainingCheckpoint.objects.get(pk=job['snapshot']['resume']['checkpoint_id'])
                env['MATTBENCH_RESUME_DIR'] = checkpoint.metadata['test_directory']
            else:
                env.pop('MATTBENCH_RESUME_DIR', None)
            result = subprocess.run([os.environ['MATTBENCH_TEST_GPU_BINARY']], env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)
            self.assertEqual(result.returncode, 0, result.stdout[-4000:])
            if 'resume' in job['snapshot']:
                self.assertIn('MATTBENCH_RESUMED %d' % job['workload']['start'], result.stdout)
            metrics = [json.loads(match) for match in re.findall(r'MATTBENCH_METRIC (\{[^\n]+\})', result.stdout)]
            self.assertEqual([metric['superbatch'] for metric in metrics], list(range(job['workload']['start'], job['workload']['end'] + 1)))
            TrainingRun.objects.filter(pk=self.run.pk).update(state='TRAINING')
            sequence = self.run.report_sequence
            for metric in metrics:
                sequence += 1
                status, response = self.request(training_api.report, {'state': 'TRAINING', 'sequence': sequence, 'metrics': metric}, pk=self.run.pk)
                self.assertEqual(status, 200, response)
            if self.run.pk == first.pk: all_metrics.extend(metrics)
            for record in re.findall(r'MATTBENCH_CHECKPOINT (\{[^\n]+\})', result.stdout):
                checkpoint_record = json.loads(record)
                sb = checkpoint_record['superbatch']; directory = output / checkpoint_record['path']
                network = directory / 'quantised.bin'
                for name in ('weights.bin', 'momentum.bin', 'velocity.bin'):
                    self.assertGreater((directory / 'optimiser_state' / name).stat().st_size, 0)
                archive = io.BytesIO()
                with tarfile.open(fileobj=archive, mode='w') as tar:
                    for file in directory.rglob('*'):
                        if file.is_file(): tar.add(file, arcname=file.relative_to(directory).as_posix())
                archive_id = self.artifact('checkpoint-%d.tar.zst' % sb, 'checkpoint', zstandard.ZstdCompressor().compress(archive.getvalue()))
                network_id = self.artifact('sb-%d.bin' % sb, 'network', network.read_bytes())
                status, response = self.request(checkpoint_ready, {'superbatch': sb, 'archive': archive_id, 'network': network_id, 'metadata': {'test_directory': str(directory)}}, pk=self.run.pk)
                self.assertEqual(status, 200, response)
            self.assertEqual(self.complete(sequence + 1)[0], 200)
            self.token = uuid.uuid4()
        first.refresh_from_db()
        self.assertEqual(first.state, 'COMPLETED')
        self.assertEqual(first.completed_superbatches, 5)
        self.assertEqual([metric['superbatch'] for metric in all_metrics], [1, 2, 3, 4, 5])
        for metric, lr, wdl in zip(all_metrics, [0.0055, 0.001, 0.000775, 0.000325, 0.0001], [0.2, 0.5, 0.8, 0.8, 0.8]):
            self.assertAlmostEqual(metric['learning_rate'], lr, places=7)
            self.assertAlmostEqual(metric['wdl_blend'], wdl, places=6)
        self.assertEqual(len(first.history), 5)
        print('GPU allocation order:', allocations)
