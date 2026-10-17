import bz2
from concurrent.futures import ThreadPoolExecutor, wait
import io
import json
from pathlib import Path
import tarfile
import tempfile
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from django.test import SimpleTestCase
from Client import training_cache, training_data
from Client.training_worker import cleanup_runs, convert_dataset, WorkerSession


class DatasetConversionTests(SimpleTestCase):
    def reporter(self, paths, skip_broken_games=True):
        reporter = Mock(
            job={
                'snapshot': {'settings': {'skip_broken_games': skip_broken_games, 'fill_missing_evals': ''}},
                'worker_info': {'threads': 2},
                'dataset': {'size': sum(path.stat().st_size for path in paths)},
            },
            lock=threading.Lock(), stop=threading.Event(), metrics={}, expanded_bytes=0, abort_reason='',
        )
        reporter.update.side_effect = lambda **values: reporter.metrics.update(values)
        return reporter

    def command(self, arguments, *args, **kwargs):
        source = Path(arguments[arguments.index('--input') + 1])
        contents = source.read_bytes()
        if arguments[1] == 'pgntovf':
            Path(arguments[arguments.index('--output') + 1]).write_bytes(b'valid-game' if contents == b'valid' else b'')
            broken = 2 if contents == b'empty' and '--skip-broken-games' in arguments else 0
            return 'broken games: %d\nparsed games: %d\n' % (broken, contents == b'valid')
        if '--check-only' in arguments:
            self.assertTrue(contents)
            return ''
        self.assertTrue(contents, 'Empty files must be skipped before sanitisation.')
        Path(arguments[arguments.index('--output') + 1]).write_bytes(contents)
        return 'parsed 1 games and skipped 0 bytes'

    def test_empty_conversions_are_skipped_in_files_and_archives(self):
        for archive in (False, True):
            for skip_broken in (False, True):
                with self.subTest(archive=archive, skip_broken=skip_broken), tempfile.TemporaryDirectory() as temp:
                    root = Path(temp)
                    members = [('7626.35810.1865729.pgn.bz2', bz2.compress(b'empty')),
                               ('valid.pgn.bz2', bz2.compress(b'valid')),
                               ('also-valid.pgn.bz2', bz2.compress(b'valid'))]
                    if archive:
                        paths = [root / 'games.tar']
                        with tarfile.open(paths[0], 'w') as output:
                            for name, contents in members:
                                member = tarfile.TarInfo(name)
                                member.size = len(contents)
                                output.addfile(member, io.BytesIO(contents))
                    else:
                        paths = [root / name for name, _ in members]
                        for path, (_, contents) in zip(paths, members):
                            path.write_bytes(contents)
                    reporter = self.reporter(paths, skip_broken)
                    with patch('Client.training_worker.command', side_effect=self.command):
                        outputs = convert_dataset(paths, root, 'pawnocchio', {}, reporter, [{'format': 'pgn'}] * len(paths))
                    self.assertEqual([path.name for path in outputs], ['00001.vf', '00002.vf'])
                    self.assertEqual([path.read_bytes() for path in outputs], [b'valid-game'] * 2)
                    self.assertEqual(set((root / 'data').iterdir()), set(outputs))
                    self.assertTrue(all(not path.exists() for path in paths))
                    self.assertFalse(reporter.stop.is_set())
                    self.assertEqual(reporter.abort_reason, '')
                    self.assertEqual(reporter.metrics['converted_files'], 2)
                    self.assertEqual(reporter.metrics['converted_bytes'], 20)
                    self.assertEqual(reporter.metrics['progress'], 100)
                    self.assertEqual(reporter.metrics.get('skipped_games', 0), 2 if skip_broken else 0)
                    self.assertIn('7626.35810.1865729.pgn.bz2', ''.join(call.args[0] for call in reporter.write.call_args_list))

    def test_all_empty_conversions_fail_after_processing_every_file(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = [root / ('%d.pgn' % index) for index in range(5)]
            for path in paths:
                path.write_bytes(b'empty')
            reporter = self.reporter(paths)
            with patch('Client.training_worker.command', side_effect=self.command) as command:
                with self.assertRaisesRegex(RuntimeError, 'No valid training games were found in the dataset'):
                    convert_dataset(paths, root, 'pawnocchio', {}, reporter, [{}] * len(paths))
            self.assertEqual(command.call_count, len(paths))
            self.assertEqual(reporter.metrics['skipped_games'], 10)
            self.assertEqual(list((root / 'data').iterdir()), [])

    def test_conversion_progress_waits_for_sanitisation_of_files_and_archive_members(self):
        for archive in (False, True):
            with self.subTest(archive=archive), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                path = root / ('games.tar' if archive else 'games.pgn')
                if archive:
                    with tarfile.open(path, 'w') as output:
                        member = tarfile.TarInfo('games.pgn')
                        member.size = 5
                        output.addfile(member, io.BytesIO(b'valid'))
                else:
                    path.write_bytes(b'valid')
                reporter = self.reporter([path])
                sanitising, draining, release = threading.Event(), threading.Event(), threading.Event()
                def command(arguments, *args, **kwargs):
                    if arguments[1] == 'sanitise':
                        sanitising.set()
                        if not release.wait(5):
                            raise RuntimeError('Test did not release sanitisation.')
                    return self.command(arguments, *args, **kwargs)
                def drain(*args, **kwargs):
                    draining.set()
                    return wait(*args, **kwargs)
                with patch('Client.training_worker.command', side_effect=command), patch('Client.training_worker.wait', side_effect=drain), ThreadPoolExecutor(max_workers=1) as executor:
                    result = executor.submit(convert_dataset, [path], root, 'pawnocchio', {}, reporter, [{'format': 'pgn'}])
                    try:
                        self.assertTrue(sanitising.wait(5))
                        self.assertTrue(draining.wait(5))
                        self.assertFalse(result.done())
                        self.assertEqual(reporter.metrics['progress'], 0)
                        self.assertEqual(reporter.metrics['progress_known'], 1)
                        if archive:
                            self.assertTrue(any(call.kwargs.get('progress_known') == 0 for call in reporter.update.call_args_list))
                    finally:
                        release.set()
                    self.assertEqual(len(result.result(timeout=5)), 1)
                self.assertEqual(reporter.metrics['progress'], 100)
                self.assertEqual(reporter.metrics['converted_files'], 1)

    def test_empty_viriformat_is_skipped(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = [root / 'empty.vf', root / 'valid.vf']
            paths[0].touch()
            paths[1].write_bytes(b'valid-game')
            reporter = self.reporter(paths)
            with patch('Client.training_worker.command', side_effect=self.command):
                outputs = convert_dataset(paths, root, 'pawnocchio', {}, reporter, [{'format': 'vf'}] * 2)
            self.assertEqual([path.read_bytes() for path in outputs], [b'valid-game'])
            self.assertFalse(reporter.stop.is_set())

    def test_missing_output_and_converter_errors_remain_fatal(self):
        for error in (None, RuntimeError('Converter failed with exit code 1.')):
            with self.subTest(error=error), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                source = root / 'source.pgn'
                source.write_bytes(b'valid')
                reporter = self.reporter([source])
                with patch('Client.training_worker.command', return_value='', side_effect=error):
                    expected = 'did not produce an output file' if error is None else 'Converter failed with exit code 1'
                    with self.assertRaisesRegex(RuntimeError, expected):
                        convert_dataset([source], root, 'pawnocchio', {}, reporter, [{}])
                self.assertTrue(reporter.stop.is_set())


class DatasetPreparationProgressTests(SimpleTestCase):
    def reporter(self):
        reporter = Mock(metrics={'progress': 100, 'progress_known': 1}, stop=threading.Event())
        reporter.update.side_effect = lambda **values: reporter.metrics.update(values)
        return reporter

    def assert_step(self, reporter, name):
        self.assertEqual(reporter.metrics['preparation_step'], name)
        self.assertEqual(reporter.metrics['progress_known'], 0)
        self.assertEqual(reporter.metrics['progress'], 0)

    def test_completed_conversion_and_upload_percentages_do_not_leak_into_other_steps(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / 'data.vf'
            path.write_bytes(b'valid-game')
            reporter = self.reporter()
            job = {'id': 1, 'snapshot': {'settings': {}}, 'dataset': {'size': 10, 'files': [{}]},
                   'worker_info': {'threads': 1, 'combiner_path': 'combiner', 'runtime': {'dataset_tools': {'combiner': 'test'}}}}
            plan = {'mode': 'prepare', 'steps': ['shuffle', 'analyse'], 'prefix': 'prepared/', 'repo': 'test/data'}
            def order(*args):
                self.assert_step(reporter, 'Shuffling and interleaving')
                return [path], 1
            def analyse(*args, **kwargs):
                self.assert_step(reporter, 'Analysing dataset')
                return 'analysis'
            def digest(*args):
                self.assert_step(reporter, 'Hashing dataset')
                return 'a' * 64
            def upload(*args):
                reporter.update(preparation_step='Uploading dataset', progress_known=1, progress=100)
            def publish(*args, **kwargs):
                self.assert_step(reporter, 'Publishing dataset')
                return Mock(json=Mock(return_value={'stored': True, 'revision': 'b' * 40}))
            connection = Mock()
            connection.request.side_effect = publish
            with patch.object(training_data, 'order_games', side_effect=order), patch.object(training_data, 'file_digest', side_effect=digest), patch.object(training_data, 'upload_prepared_files', side_effect=upload):
                selected = training_data.prepare_and_publish(connection, job, plan, [path], root, 'pawnocchio', {}, reporter, analyse)
            self.assertEqual(selected, [path])
            self.assertEqual(job['dataset_publication']['revision'], 'b' * 40)

    def test_upload_is_not_complete_until_the_server_confirms_it(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'data.vf'
            path.write_bytes(b'valid-game')
            reporter = self.reporter()
            descriptor = {'path': 'data.vf', 'size': 10, 'sha256': 'a' * 64}
            def request(*args, **kwargs):
                if kwargs['json'].get('verify'):
                    self.assertEqual(reporter.metrics['uploaded_bytes'], 10)
                    self.assertEqual(reporter.metrics['progress'], 0)
                    result = {'stored': True}
                else:
                    result = {'upload': {'href': 'https://unused.invalid/upload'}}
                return Mock(json=Mock(return_value=result))
            def put(url, data, **kwargs):
                while data.read():
                    pass
                self.assertEqual(reporter.metrics['progress'], 0)
                return MagicMock()
            connection = Mock()
            connection.request.side_effect = request
            with patch.object(training_data.requests, 'put', side_effect=put):
                training_data.upload_prepared_files(connection, {'id': 1}, [path], [descriptor], reporter)
            self.assertEqual(reporter.metrics['progress'], 100)
            self.assertEqual(reporter.metrics['uploaded_files'], 1)


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
