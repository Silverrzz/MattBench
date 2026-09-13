import bz2
import io
import json
from pathlib import Path
import tarfile
import tempfile
import threading
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase
from Client import training_cache
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
