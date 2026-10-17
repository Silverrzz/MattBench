import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, TestCase

from Client import training_worker as worker
from OpenBench.tests.test_training_workloads import WorkloadFixture


class WorkerMultiDatasetTests(SimpleTestCase):
    def job(self):
        sources = [{'repo': 'test/a', 'commit': 'a' * 40, 'file_indices': [0]},
                   {'repo': 'test/b', 'commit': 'b' * 40, 'file_indices': [1]}]
        return {'id': 1, 'snapshot': {'settings': {}}, 'dataset': {'files': [
            {'path': 'data.vf', 'size': 100}, {'path': 'data.vf', 'size': 100}],
            'stages': [{'start': 1, 'end': 10, 'sources': sources},
                       {'start': 11, 'end': 20, **sources[0]}]}}

    def prepare(self, job):
        connection = worker.Connection('http://test', 'worker', 'token')
        reporter = Mock(state='CONVERTING')
        def download(connection, source_job, directory, reporter):
            path = directory / 'data.vf'
            path.write_bytes(b'data')
            return [path]
        def publish(connection, source_job, plan, paths, *args):
            source_job['dataset_statistics'] = {'games': 10}
            return paths
        def plan(connection, *args, **kwargs):
            return Mock(json=Mock(return_value={'steps': [], 'source_key': connection.url('dataset')}))
        with tempfile.TemporaryDirectory() as root, patch.object(worker.StageConnection, 'request', autospec=True, side_effect=plan) as request, patch.object(worker, 'download_dataset', side_effect=download) as downloads, patch.object(worker, 'prepare_and_publish', side_effect=publish):
            stage_data, all_data = worker.prepare_stage_datasets(connection, job, Path(root), reporter, 'pawnocchio', {}, Path(root), [])
            return stage_data, all_data, request.call_args_list, downloads.call_args_list

    def test_each_source_is_prepared_once_and_stage_receives_all_paths(self):
        job = self.job()
        stages, paths, plans, downloads = self.prepare(job)
        self.assertEqual([len(stage) for stage in stages], [2, 1])
        self.assertEqual(len(paths), 2)
        self.assertEqual(stages[1], [stages[0][0]])
        self.assertEqual([call.args[1]['dataset']['repo'] for call in downloads], ['test/a', 'test/b'])
        self.assertEqual([call.args[0].url('dataset') for call in plans],
                         ['http://test/api/training/dataset?stage=0.0', 'http://test/api/training/dataset?stage=0.1'])
        self.assertEqual(job['dataset_statistics']['games'], 20)

    def test_completed_stages_are_skipped_when_resuming(self):
        job = self.job()
        job['snapshot']['resume'] = {'superbatch': 10}
        stages, paths, plans, downloads = self.prepare(job)
        self.assertEqual([len(stage) for stage in stages], [0, 1])
        self.assertEqual(len(downloads), 1)
        self.assertEqual(plans[0].args[0].url('dataset'), 'http://test/api/training/dataset?stage=1')

    def test_legacy_stage_shape_keeps_legacy_preparation_endpoint(self):
        job = self.job()
        job['dataset']['stages'] = [job['dataset']['stages'][1]]
        stages, paths, plans, downloads = self.prepare(job)
        self.assertEqual([len(stage) for stage in stages], [1])
        self.assertEqual(plans[0].args[0].url('dataset'), 'http://test/api/training/dataset?stage=0')


class MultiDatasetAllocationTests(WorkloadFixture, TestCase):
    def test_old_workers_keep_single_dataset_jobs_and_new_workers_get_multi_dataset_jobs(self):
        source = {key: value for key, value in self.dataset.items() if key != 'files'}
        source['file_indices'] = [0]
        self.run.dataset = {**self.dataset, 'stages': [{'start': 1, 'end': 5,
                            'sources': [source, {**source, 'repo': 'other/data'}]}]}
        self.run.save(update_fields=['dataset'])
        self.assertIsNone(self.claim()['run'])
        from OpenBench.worker_diagnostics import training_reasons
        reasons = training_reasons(self.worker, {}, self.run)
        self.assertIn('Update this worker to support multiple datasets per stage.', reasons)
        self.worker.info['capabilities'].append('multi-dataset-stages')
        self.worker.save(update_fields=['info'])
        self.assertEqual(self.claim()['run']['id'], self.run.pk)
