import copy
import importlib.util
from pathlib import Path
import tempfile
from unittest.mock import Mock, patch

from django.test import SimpleTestCase


loader = importlib.util.spec_from_file_location('builder_update', Path(__file__).resolve().parents[2] / 'Deploy/update-builder-schedule.py')
updater = importlib.util.module_from_spec(loader)
loader.loader.exec_module(updater)


class BuilderUpdateTests(SimpleTestCase):
    url = 'https://example.invalid/training/schedules/id/builder/'

    def before(self):
        return dict(id='id', version=1, notice='', post_url='/training/schedules/id/builder/',
                    name='Network', scope='personal', engine='',
                    spec=dict(feature_export={}, optimizer='adamw', export_mode='legacy'))

    def test_preview_backup_save_and_readback(self):
        before = self.before()
        after = copy.deepcopy(before)
        after.update(version=2)
        after['spec']['export_mode'] = 'custom'
        session = Mock()
        session.post.return_value.status_code = 200
        session.post.return_value.json.return_value = {'id': 'id'}
        with tempfile.TemporaryDirectory() as directory, patch.object(updater, 'payload', side_effect=[before, after]):
            backup = Path(directory) / 'before.json'
            updater.update(session, self.url, 'test-csrf', 1, {'export_mode': 'custom'}, backup)
            self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
            self.assertEqual([call.kwargs['json']['action'] for call in session.post.call_args_list], ['preview', 'save'])
            self.assertEqual(session.post.call_args.kwargs['json']['version'], 1)

    def test_stale_or_old_builder_never_posts(self):
        for changes in ({'version': 2}, {'id': ''}, {'notice': 'Source edited'},
                        {'spec': {'export_mode': 'legacy'}}, {'post_url': '/different/'}):
            before = {**self.before(), **changes}
            session = Mock()
            with self.subTest(changes=changes), patch.object(updater, 'payload', return_value=before), self.assertRaises(RuntimeError):
                updater.update(session, self.url, 'test-csrf', 1, {'export_mode': 'custom'}, Path('/unused'))
            session.post.assert_not_called()

    def test_check_and_idempotent_runs_do_not_save(self):
        session = Mock()
        session.post.return_value.status_code = 200
        with patch.object(updater, 'payload', return_value=self.before()):
            updater.update(session, self.url, 'test-csrf', 1, {'export_mode': 'custom'}, Path('/unused'), check=True)
        self.assertEqual(session.post.call_count, 1)
        session.reset_mock()
        with patch.object(updater, 'payload', return_value=self.before()):
            updater.update(session, self.url, 'test-csrf', 1, {'export_mode': 'legacy'}, Path('/unused'))
        session.post.assert_not_called()
