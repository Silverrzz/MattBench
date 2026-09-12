import json
import tempfile
import threading
import uuid
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests
from cryptography.fernet import Fernet
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import transaction
from django.test import Client, RequestFactory, TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from OpenBench import discord_notifications as discord
from OpenBench.lifecycle import record_event, test_event
from OpenBench.models import (DatasetUpload, DiscordConfiguration, Engine, EngineConfig, LifecycleEvent, Machine,
                              NotificationDelivery, NotificationPreferences, Profile, Result, SPSARun, Test,
                              TrainingArtifact, TrainingRun, TrainingWorker)
from OpenBench.notification_events import EVENTS, default_events
from OpenBench.tests.test_training_workloads import WorkloadFixture
from OpenBench.tests import test_training_workloads as workload_tests

WEBHOOK = 'https://discord.com/api/webhooks/123456789012345678/' + 'x' * 68
DISCORD_ID = '234567890123456789'
KEY = Fernet.generate_key().decode()


class NotificationFixture:
    def setUp(self):
        self.keys = override_settings(TRAINING_CREDENTIAL_KEY=KEY, TRAINING_CREDENTIAL_KEY_FILE='')
        self.keys.enable()
        self.addCleanup(self.keys.disable)
        self.user = User.objects.create_user('alice', password='test-password')
        self.admin = User.objects.create_user('admin', is_superuser=True)
        Profile.objects.create(user=self.user, enabled=True)
        Profile.objects.create(user=self.admin, enabled=True, approver=True)
        self.config = DiscordConfiguration.objects.create(enabled=True, webhook_ciphertext=discord.encrypt_webhook(WEBHOOK),
            canonical_url='https://bench.example.com', destination_label='Bench results', guild_id=DISCORD_ID, channel_id=DISCORD_ID)
        self.prefs = NotificationPreferences.objects.create(user=self.user, enabled=True, events={key: 'message' for key in EVENTS})
        self.engine = Engine.objects.create(name='Improve pruning', sha='a' * 40)
        self.machine = Machine.objects.create(user=self.user, info={'machine_name': 'workstation-01'})
        self.factory = RequestFactory()

    def make_test(self, **kwargs):
        return Test.objects.create(author=self.user.username, dev=self.engine, base=self.engine, dev_engine='Example',
                                   max_games=10, test_mode=kwargs.pop('test_mode', 'GAMES'), **kwargs)

    def event(self, action='completed'):
        test = self.make_test(test_mode='DATAGEN')
        test_event(action, test)
        return NotificationDelivery.objects.latest('id')

    def post(self, view, data, *args, user=None):
        request = self.factory.post('/', data)
        request.user = user or self.user
        request.session = {}
        return view(request, *args)

    def response(self, code=200, body=None, headers=None):
        return Mock(status_code=code, headers=headers or {}, json=Mock(return_value=body if body is not None else {'id': DISCORD_ID}))


class EventTests(NotificationFixture, TestCase):
    def test_all_registered_event_cards_and_legacy_normalization(self):
        engine = EngineConfig.objects.create(name='Example')
        run = TrainingRun.objects.create(owner=self.user, engine=engine, name='Experiment', metrics={'loss': .23, 'end_superbatch': 400})
        upload = DatasetUpload.objects.create(owner=self.user, workload=self.make_test(test_mode='DATAGEN'), repo='private/repo', filename='secret/path')
        subjects = {'test': self.make_test(), 'tune': self.make_test(test_mode='SPSA'), 'datagen': self.make_test(test_mode='DATAGEN'),
                    'training': run, 'upload': upload, 'worker': self.machine}
        for key in EVENTS:
            family = key.split('.')[0]
            event = record_event(key, subjects[family], self.user.pk, {'logs': 'secret log', 'token': 'secret token'}, key=key)
            row = NotificationDelivery.objects.get(event=event)
            card = row.summary
            self.assertTrue(card['url'].startswith('https://bench.example.com/'))
            self.assertLessEqual(len(card['title']), 256)
            self.assertIn('Owner', [field['name'] for field in card['fields']])
            for secret in ('secret log', 'secret token', 'private/repo', 'secret/path'):
                self.assertNotIn(secret, json.dumps(card))
        self.assertEqual(NotificationDelivery.objects.count(), len(EVENTS))
        for kind, subject, expected in [('datagen.uploaded', upload, 'upload.completed'), ('training.task.failed', upload, 'upload.failed'),
                                        ('training.task.failed', run, 'training.failed'), ('training.resumed', run, 'training.continued'),
                                        ('training.cancel.requested', run, 'training.cancel_requested')]:
            event = record_event(kind, subject, self.user.pk)
            self.assertEqual(NotificationDelivery.objects.get(event=event).event_key, expected)

    def test_defaults_and_opt_in_no_historical_replay(self):
        self.config.enabled = False; self.config.save()
        test = self.make_test()
        event = test_event('passed', test)
        self.config.enabled = True; self.config.save()
        test_event('passed', test)
        self.assertFalse(NotificationDelivery.objects.filter(event=event).exists())
        self.prefs.enabled = False; self.prefs.save()
        test_event('failed', self.make_test())
        self.assertEqual(NotificationDelivery.objects.count(), 0)
        defaults = NotificationPreferences(user=self.admin)
        self.assertFalse(defaults.enabled)
        self.assertEqual(defaults.events['training.interrupted'], 'message')
        self.assertEqual(defaults.events['worker.registered'], 'off')

    def test_owner_actor_routing_and_transaction_rollback(self):
        test = self.make_test()
        event = test_event('approved', test, self.admin.pk)
        row = NotificationDelivery.objects.get(event=event)
        self.assertEqual(row.recipient, self.user)
        self.assertEqual(event.data['actor_id'], self.admin.pk)
        self.assertEqual(event.data['subject_owner_id'], self.user.pk)
        self.assertIn('admin', json.dumps(row.summary))
        with self.assertRaises(RuntimeError), transaction.atomic():
            test.finished = True; test.save()
            test_event('passed', test)
            raise RuntimeError
        test.refresh_from_db()
        self.assertFalse(test.finished)
        self.assertEqual(NotificationDelivery.objects.count(), 1)
        self.assertEqual(LifecycleEvent.objects.count(), 1)

    def test_unresolved_legacy_author_is_skipped_with_diagnostic(self):
        test = self.make_test(); test.author = 'deleted-account'; test.save()
        with self.assertLogs('OpenBench.lifecycle', 'WARNING'):
            self.assertIsNone(test_event('passed', test))
        self.assertEqual(NotificationDelivery.objects.count(), 0)

    def test_controls_repeat_only_after_real_transitions(self):
        from OpenBench.workloads.modify_workload import modify_workload
        test = self.make_test()
        for action in ('APPROVE', 'APPROVE', 'STOP', 'STOP', 'RESTART', 'RESTART', 'STOP', 'RESTART', 'DELETE', 'DELETE', 'RESTORE', 'DELETE', 'RESTORE'):
            self.post(modify_workload, {}, test.pk, action, user=self.admin)
        counts = {key: NotificationDelivery.objects.filter(event_key='test.' + key).count() for key in ('approved', 'stopped', 'restarted', 'deleted', 'restored')}
        self.assertEqual(counts, {'approved': 1, 'stopped': 2, 'restarted': 2, 'deleted': 2, 'restored': 2})
        test.refresh_from_db(); self.assertEqual(test.execution_number, 2)

    def test_first_assignment_restart_and_preexisting_assignments(self):
        from OpenBench.workloads.get_workload import get_workload
        from OpenBench.workloads.modify_workload import modify_workload
        test = self.make_test(approved=True)
        request = self.factory.post('/')
        with patch('OpenBench.workloads.get_workload.select_workload', return_value=test), patch('OpenBench.workloads.get_workload.workload_to_dictionary', return_value={'test': {'id': test.pk}}):
            get_workload(request, self.machine); get_workload(request, self.machine)
            self.post(modify_workload, {}, test.pk, 'STOP')
            self.post(modify_workload, {}, test.pk, 'RESTART')
            get_workload(request, self.machine)
            self.assertEqual(NotificationDelivery.objects.filter(event_key='test.started').count(), 2)
        historical = self.make_test(approved=True)
        Result.objects.create(test=historical, machine=self.machine)
        with patch('OpenBench.workloads.get_workload.select_workload', return_value=historical), patch('OpenBench.workloads.get_workload.workload_to_dictionary', return_value={}):
            get_workload(request, self.machine)
        self.assertFalse(NotificationDelivery.objects.filter(subject_key='test:%s' % historical.pk).exists())

    def test_execution_errors_consolidate_reset_and_exclude_game_errors(self):
        from OpenBench.views import client_bench_error, client_submit_error
        from OpenBench.workloads.modify_workload import modify_workload
        test = self.make_test()
        from OpenBench.config import OPENBENCH_CONFIG, eligibility_fingerprint
        self.machine.info.update(client_ver=OPENBENCH_CONFIG['client_version'], OPENBENCH_CONFIG_CHECKSUM=eligibility_fingerprint())
        self.machine.save()
        bench = client_bench_error
        request = self.factory.post('/', {'test_id': test.pk, 'machine_id': self.machine.pk, 'error': 'bench mismatch', 'secret': self.machine.secret})
        bench(request); bench(request)
        self.assertEqual(NotificationDelivery.objects.filter(event_key='test.execution_error').count(), 1)
        self.assertIn('Execution stopped', NotificationDelivery.objects.get(event_key='test.execution_error').summary['description'])
        self.post(modify_workload, {}, test.pk, 'RESTART')
        with tempfile.TemporaryDirectory() as media, override_settings(MEDIA_ROOT=media):
            report = client_submit_error
            request = self.factory.post('/', {'test_id': test.pk, 'machine_id': self.machine.pk, 'error': 'Illegal move', 'logs': 'private logs', 'secret': self.machine.secret})
            report(request)
            self.assertEqual(NotificationDelivery.objects.filter(event_key='test.execution_error').count(), 1)
            request.POST = request.POST.copy(); request.POST['error'] = '[Example] main build failed'
            report(request); report(request)
        rows = NotificationDelivery.objects.filter(event_key='test.execution_error').order_by('id')
        self.assertEqual(rows.count(), 2)
        self.assertIn('remains active', rows.last().summary['description'])

    def test_outcomes_and_replayed_final_results(self):
        from OpenBench.utils import update_test
        for mode, tri, expected in [('GAMES', '2 4 4', 'test.passed'), ('GAMES', '4 4 2', 'test.failed'),
                                     ('SPRT', '2 4 4', 'test.passed'), ('SPSA', '2 4 4', 'tune.completed'),
                                     ('DATAGEN', '2 4 4', 'datagen.completed')]:
            test = self.make_test(test_mode=mode)
            if mode == 'SPSA':
                SPSARun.objects.create(tune=test, iterations=1, pairs_per=5, reporting_type='BULK', alpha=.6, gamma=.1, a_ratio=.1)
            result = Result.objects.create(test=test, machine=self.machine)
            request = self.factory.post('/', {'test_id': test.pk, 'result_id': result.pk, 'machine_id': self.machine.pk,
                'crashes': 0, 'timelosses': 0, 'illegals': 0, 'trinomial': tri, 'pentanomial': '1 1 1 1 1'})
            with patch('OpenBench.utils.PentanomialSPRT', return_value=3):
                update_test(request, self.machine); update_test(request, self.machine)
            self.assertEqual(list(NotificationDelivery.objects.filter(subject_key='test:%s' % test.pk).values_list('event_key', flat=True)), [expected])

    def test_worker_modes_settings_and_disconnect(self):
        from OpenBench.worker_views import detail
        for mode in ('paused', 'paused', 'automatic', 'automatic', 'paused', 'training-only'):
            self.post(detail, {'action': 'mode', 'mode': mode}, self.machine.pk, user=self.admin)
        self.assertEqual(NotificationDelivery.objects.filter(event_key='worker.pause_requested').count(), 2)
        self.assertEqual(NotificationDelivery.objects.filter(event_key='worker.resumed').count(), 2)
        self.post(detail, {'action': 'settings', 'name': 'Workstation', 'mode': 'paused'}, self.machine.pk)
        self.assertEqual(NotificationDelivery.objects.filter(event_key='worker.pause_requested').count(), 3)
        self.post(detail, {'action': 'disconnect'}, self.machine.pk, user=self.admin)
        row = NotificationDelivery.objects.get(event_key='worker.disconnected')
        self.assertEqual(row.recipient_id, self.user.pk)
        self.assertEqual(row.event.data['actor_id'], self.admin.pk)

    def test_worker_registration_and_capability_link_are_not_duplicate_alerts(self):
        from OpenBench import training_api, views
        credentials = {'username': self.user.username, 'password': 'test-password'}
        with patch.object(views, 'OPENBENCH_CONFIG', {'client_version': 1, 'engines': {}}):
            response = self.post(views.client_worker_info, {**credentials, 'system_info': json.dumps({'client_ver': 1, 'concurrency': 2})})
            identity = json.loads(response.content)
        self.assertEqual(NotificationDelivery.objects.filter(event_key='worker.registered').count(), 1)
        info = {'protocol': 3, 'backend': 'cuda', 'vram_gb': 8, 'disk_gb': 100, 'threads': 2,
                'pawnocchio_sha256': 'a' * 64, 'runtime': {'worker_sha256': 'b' * 64}}
        data = {**credentials, 'machine_id': identity['machine_id'], 'machine_secret': identity['secret'],
                'name': 'Workstation', 'worker': str(uuid.uuid4()), 'token': 'c' * 64, 'info': json.dumps(info)}
        for _ in range(2):
            response = self.post(training_api.register, data)
            self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(NotificationDelivery.objects.filter(event_key='worker.registered').count(), 1)
        data.pop('machine_id'); data.pop('machine_secret'); data['worker'] = str(uuid.uuid4())
        self.assertEqual(self.post(training_api.register, data).status_code, 200)
        self.assertEqual(NotificationDelivery.objects.filter(event_key='worker.registered').count(), 2)

    def test_upload_retries_exhaustion_and_completion(self):
        from OpenBench.training_tasks import TrainingTasks, upload_dataset
        task = DatasetUpload.objects.create(owner=self.user, workload=self.make_test(test_mode='DATAGEN'), repo='private/data', filename='data.tar', state='UPLOADING', task_attempts=1)
        service = TrainingTasks(threading.Event())
        service.failed(task, requests.Timeout('secret url'), 'UPLOADING', 'QUEUED', 'Dataset upload')
        self.assertEqual(NotificationDelivery.objects.count(), 0)
        task.state = 'UPLOADING'; task.task_attempts = 5; task.save()
        service.failed(task, requests.Timeout(), 'UPLOADING', 'QUEUED', 'Dataset upload')
        service.failed(task, requests.Timeout(), 'UPLOADING', 'QUEUED', 'Dataset upload')
        self.assertEqual(NotificationDelivery.objects.filter(event_key='upload.failed').count(), 1)
        task.state = 'UPLOADING'; task.save()
        with tempfile.TemporaryDirectory() as root, override_settings(MEDIA_ROOT=root), patch('OpenBench.training_tasks.hf_token', return_value='secret'), patch('huggingface_hub.HfApi') as api, patch('OpenBench.dataset_manifest.read_manifest', return_value={'version': 1, 'files': []}):
            path = Path(root) / 'PGNs'; path.mkdir(); (path / ('%s.pgn.tar' % task.workload_id)).write_bytes(b'archive')
            api.return_value.dataset_info.return_value = SimpleNamespace(private=True, sha='a' * 40)
            api.return_value.get_paths_info.return_value = []
            api.return_value.create_commit.return_value = SimpleNamespace(oid='b' * 40)
            upload_dataset(task)
        self.assertEqual(NotificationDelivery.objects.filter(event_key='upload.completed').count(), 1)

    def test_coordinator_attempt_limits_emit_failures(self):
        from OpenBench.training_tasks import TrainingTasks
        engine = EngineConfig.objects.create(name='Example')
        TrainingRun.objects.create(owner=self.user, engine=engine, name='Validation', task_attempts=5)
        DatasetUpload.objects.create(owner=self.user, workload=self.make_test(test_mode='DATAGEN'), repo='repo', filename='file', task_attempts=5)
        service = TrainingTasks(threading.Event())
        service.validate(); service.validate(); service.upload(); service.upload()
        self.assertEqual(list(NotificationDelivery.objects.order_by('id').values_list('event_key', flat=True)), ['training.failed', 'upload.failed'])


class SenderTests(NotificationFixture, TestCase):
    def test_mentions_safe_payload_and_confirmation(self):
        self.prefs.events['datagen.completed'] = 'ping'; self.prefs.discord_user_id = DISCORD_ID; self.prefs.save()
        test = self.make_test(test_mode='DATAGEN', info='@everyone <@111111111111111111> [trick](https://evil.example)')
        test_event('completed', test)
        with patch.object(discord.requests, 'post', return_value=self.response()) as send:
            self.assertTrue(discord.send_once())
        payload = send.call_args.kwargs['json']
        self.assertEqual(payload['content'], '<@%s>' % DISCORD_ID)
        self.assertEqual(payload['allowed_mentions'], {'parse': [], 'users': [DISCORD_ID], 'roles': [], 'replied_user': False})
        self.assertNotIn('@everyone', payload['embeds'][0]['title'])
        self.assertEqual(send.call_args.kwargs['params'], {'wait': 'true'})
        self.assertFalse(send.call_args.kwargs['allow_redirects'])
        row = NotificationDelivery.objects.get()
        self.assertEqual((row.status, row.message_id, row.attempts), ('sent', DISCORD_ID, 1))
        self.config.refresh_from_db(); self.assertIsNotNone(self.config.last_success)

    def test_preference_recheck_removes_mentions_and_skips_disabled(self):
        self.prefs.events['datagen.completed'] = 'ping'; self.prefs.discord_user_id = DISCORD_ID; self.prefs.save()
        row = self.event()
        self.prefs.events['datagen.completed'] = 'message'; self.prefs.save()
        with patch.object(discord.requests, 'post', return_value=self.response()) as send:
            discord.send_once()
            self.assertEqual(send.call_args.kwargs['json']['content'], '')
        row = self.event()
        self.prefs.enabled = False; self.prefs.save()
        with patch.object(discord.requests, 'post') as send:
            self.assertFalse(discord.send_once()); send.assert_not_called()
        row.refresh_from_db(); self.assertEqual(row.status, 'skipped')

    def test_generation_replacement_and_platform_disable(self):
        for changes in ({'generation': uuid.uuid4()}, {'enabled': False}):
            row = self.event()
            DiscordConfiguration.objects.filter(pk=1).update(**changes)
            with patch.object(discord.requests, 'post') as send:
                discord.send_once(); send.assert_not_called()
            row.refresh_from_db(); self.assertEqual(row.status, 'skipped')

    def test_rate_limit_honors_fractional_retry_and_subject_order(self):
        row = self.event('created')
        test = Test.objects.get(pk=row.event.subject_id); test_event('completed', test)
        with patch.object(discord.requests, 'post', return_value=self.response(429, {'retry_after': 7.25})) as send:
            discord.send_once(); discord.send_once()
            self.assertEqual(send.call_count, 1)
        row.refresh_from_db(); self.config.refresh_from_db()
        self.assertEqual(row.status, 'pending')
        self.assertGreater(self.config.next_send_at, timezone.now() + timedelta(seconds=6))
        DiscordConfiguration.objects.filter(pk=1).update(next_send_at=timezone.now())
        self.assertIsNone(discord.claim_delivery())  # Later event for same subject cannot pass retry.
        NotificationDelivery.objects.filter(pk=row.pk).update(next_attempt_at=timezone.now())
        with patch.object(discord.requests, 'post', return_value=self.response()) as send:
            discord.send_once(); discord.send_once()
            self.assertEqual(send.call_count, 2)
            self.assertIn('created', send.call_args_list[0].kwargs['json']['embeds'][0]['title'])
            self.assertIn('completed', send.call_args_list[1].kwargs['json']['embeds'][0]['title'])

    def test_retry_bound_permanent_errors_and_sanitization(self):
        for code in (301, 400, 401, 403, 404):
            row = self.event()
            with patch.object(discord.requests, 'post', return_value=self.response(code)):
                discord.send_once()
            row.refresh_from_db(); self.assertEqual(row.status, 'failed')
        row = self.event()
        NotificationDelivery.objects.filter(pk=row.pk).update(attempts=7)
        with patch.object(discord.requests, 'post', side_effect=requests.Timeout(WEBHOOK)):
            discord.send_once()
        row.refresh_from_db()
        self.assertEqual((row.status, row.attempts), ('failed', 8))
        self.assertNotIn(WEBHOOK, row.last_error)
        row = self.event()
        NotificationDelivery.objects.filter(pk=row.pk).update(created=timezone.now() - timedelta(hours=25))
        with patch.object(discord.requests, 'post') as send:
            discord.send_once(); send.assert_not_called()
        row.refresh_from_db(); self.assertEqual(row.status, 'failed')

    def test_transient_and_missing_confirmation_retry(self):
        for response in (self.response(503), self.response(200, {})):
            row = self.event()
            with patch.object(discord.requests, 'post', return_value=response):
                discord.send_once()
            row.refresh_from_db(); self.assertEqual(row.status, 'pending')
            self.assertGreater(row.next_attempt_at, timezone.now())
            NotificationDelivery.objects.filter(pk=row.pk).update(status='skipped')

    def test_crashed_sender_lease_recovery_and_exclusive_claim(self):
        row = self.event()
        first, _ = discord.claim_delivery()
        self.assertEqual(row.pk, first.pk)
        self.assertIsNone(discord.claim_delivery())
        past = timezone.now() - timedelta(seconds=1)
        DiscordConfiguration.objects.filter(pk=1).update(lease_until=past)
        NotificationDelivery.objects.filter(pk=row.pk).update(claimed_until=past)
        with patch.object(discord.requests, 'post', return_value=self.response()):
            discord.send_once()
        row.refresh_from_db(); self.assertEqual((row.status, row.attempts), ('sent', 2))

    def test_webhook_validation_masking_and_no_redirects(self):
        invalid = ['http://discord.com/api/webhooks/1/token', WEBHOOK + '?wait=true', WEBHOOK + '#x', WEBHOOK.replace('discord.com', 'discord.com.evil.example'), WEBHOOK.replace('discord.com', 'user@discord.com'), WEBHOOK.replace('/api/', ':443/api/'), WEBHOOK + '/']
        for url in invalid:
            with self.assertRaises(ValidationError):
                discord.canonical_webhook(url)
        body = {'id': WEBHOOK.split('/')[-2], 'type': 1, 'guild_id': DISCORD_ID, 'channel_id': DISCORD_ID}
        with patch.object(discord.requests, 'get', return_value=self.response(body=body)) as get:
            self.assertEqual(discord.validate_webhook(WEBHOOK), (DISCORD_ID, DISCORD_ID))
            self.assertFalse(get.call_args.kwargs['allow_redirects'])
        for response in (self.response(302), self.response(body={'type': 2}), self.response(body=[])):
            with patch.object(discord.requests, 'get', return_value=response), self.assertRaises(ValidationError):
                discord.validate_webhook(WEBHOOK)
        with patch.object(discord.requests, 'get', side_effect=requests.Timeout(WEBHOOK)):
            try:
                discord.validate_webhook(WEBHOOK)
            except ValidationError as error:
                self.assertNotIn('x' * 68, str(error))
        self.assertNotIn('x' * 68, self.config.webhook_ciphertext)
        self.assertEqual(discord.decrypt_webhook(self.config), WEBHOOK)


class SettingsTests(NotificationFixture, TestCase):
    def test_profile_permissions_csrf_defaults_and_grouped_layout(self):
        self.assertEqual(self.client.post('/profile/notifications/', {}).status_code, 302)
        self.client.force_login(self.user)
        response = self.client.get('/profile/')
        self.assertContains(response, 'Selected summaries are posted into a shared channel')
        self.assertContains(response, 'data-notification-group', count=6)
        self.assertContains(response, 'name="test.passed"')
        self.assertEqual(self.client.get('/profile/notifications/').status_code, 405)
        csrf = Client(enforce_csrf_checks=True); csrf.force_login(self.user)
        self.assertEqual(csrf.post('/profile/notifications/', {}).status_code, 403)
        data = {**default_events(), 'enabled': 'on', 'training.failed': 'ping'}
        self.assertEqual(self.client.post('/profile/notifications/', data).status_code, 400)
        for invalid in ('123', DISCORD_ID + '\n@everyone', '<@%s>' % DISCORD_ID):
            self.assertEqual(self.client.post('/profile/notifications/', {**data, 'discord_user_id': invalid}).status_code, 400)
        self.assertEqual(self.client.post('/profile/notifications/', {**data, 'discord_user_id': DISCORD_ID}).status_code, 302)
        self.prefs.refresh_from_db(); self.assertEqual(self.prefs.events['training.failed'], 'ping')

    def test_account_and_event_disable_skip_pending_even_after_reenable(self):
        self.client.force_login(self.user)
        row = self.event()
        self.client.post('/profile/notifications/', {**default_events(), 'enabled': 'on', 'datagen.completed': 'off'})
        self.client.post('/profile/notifications/', {**default_events(), 'enabled': 'on'})
        row.refresh_from_db(); self.assertEqual(row.status, 'skipped')
        row = self.event()
        self.client.post('/profile/notifications/', default_events())
        row.refresh_from_db(); self.assertEqual(row.status, 'skipped')

    def test_admin_permissions_secret_masking_replace_test_and_remove(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.get('/manage/notifications/').status_code, 403)
        self.assertEqual(self.client.post('/manage/notifications/', {'action': 'test'}).status_code, 403)
        self.client.force_login(self.admin)
        response = self.client.get('/manage/notifications/')
        self.assertContains(response, 'Sender status')
        self.assertNotContains(response, 'x' * 68)
        self.assertNotContains(response, self.config.webhook_ciphertext)
        row = self.event()
        with patch('OpenBench.notification_views.validate_webhook', return_value=(DISCORD_ID, DISCORD_ID)), patch.object(discord.requests, 'post') as send:
            response = self.client.post('/manage/notifications/', {'action': 'save', 'destination_label': 'New channel', 'canonical_url': 'https://bench.example.com', 'webhook': WEBHOOK})
            self.assertEqual(response.status_code, 302)
            response = self.client.post('/manage/notifications/', {'action': 'test'})
            self.assertEqual(response.status_code, 302)
            send.assert_not_called()
        self.config.refresh_from_db(); self.assertFalse(self.config.enabled)
        row.refresh_from_db(); self.assertEqual(row.status, 'skipped')
        with patch.object(discord.requests, 'post', return_value=self.response()) as send:
            discord.send_once()
            self.assertEqual(send.call_args.kwargs['json']['allowed_mentions']['users'], [])
            self.assertIn('test', send.call_args.kwargs['json']['embeds'][0]['title'])
        self.client.post('/manage/notifications/', {'action': 'remove'})
        self.config.refresh_from_db(); self.assertEqual(self.config.webhook_ciphertext, '')

    def test_canonical_origin_required_and_error_never_echoes_webhook(self):
        self.client.force_login(self.admin)
        for url in ('http://bench.example.com', 'https://user:pass@bench.example.com', 'https://bench.example.com/path', 'https://bench.example.com/?token=secret'):
            response = self.client.post('/manage/notifications/', {'action': 'save', 'canonical_url': url, 'destination_label': 'Channel', 'webhook': WEBHOOK})
            self.assertEqual(response.status_code, 400)
            self.assertNotContains(response, 'x' * 68, status_code=400)


class TrainingNotificationTests(WorkloadFixture, TestCase):
    def setUp(self):
        super().setUp()
        DiscordConfiguration.objects.create(enabled=True, webhook_ciphertext='not-used', canonical_url='https://bench.example.com')
        NotificationPreferences.objects.create(user=self.user, enabled=True, events={key: 'message' for key in EVENTS})

    def keys(self):
        return list(NotificationDelivery.objects.order_by('id').values_list('event_key', flat=True))

    def test_chunk_completion_silent_until_final_and_start_once(self):
        workload_tests.WorkloadTests.test_claim_complete_rotate_resume_and_retry(self)
        self.assertEqual(self.keys(), ['training.started', 'training.completed'])

    def test_recovery_episodes_and_cancelled_expiry(self):
        from OpenBench.training_workloads import expire_workload
        self.claim()
        for _ in range(2):
            expire_workload(self.run); expire_workload(self.run)
            self.token = uuid.uuid4(); self.claim()
        self.assertEqual(self.keys(), ['training.started', 'training.interrupted', 'training.recovered', 'training.interrupted', 'training.recovered'])
        TrainingRun.objects.filter(pk=self.run.pk).update(cancel_requested=True)
        expire_workload(self.run); expire_workload(self.run)
        self.assertEqual(self.keys()[-1], 'training.cancelled')
        self.assertEqual(self.keys().count('training.cancelled'), 1)

    def test_legacy_completion_and_replayed_report(self):
        from OpenBench import training_api
        self.run.workload_size = 0; self.run.save()
        self.claim()
        TrainingRun.objects.filter(pk=self.run.pk).update(state='SAVING')
        for kind in ('network', 'log', 'manifest'):
            TrainingArtifact.objects.create(run=self.run, kind=kind, name=kind, size=1, path='private/path', sha256='a' * 64)
        for _ in range(2):
            status, body = self.request(training_api.report, {'state': 'COMPLETED', 'sequence': 1}, pk=self.run.pk)
            self.assertEqual(status, 200, body)
        self.assertEqual(self.keys(), ['training.started', 'training.completed'])

    def test_legacy_interruption_recovery_and_replayed_recovery(self):
        from OpenBench import training_api
        from OpenBench.training_tasks import TrainingTasks
        self.run.workload_size = 0; self.run.save(); self.claim()
        TrainingRun.objects.filter(pk=self.run.pk).update(updated=timezone.now() - timedelta(hours=1))
        service = TrainingTasks(threading.Event()); service.maintain(); service.maintain()
        for _ in range(2):
            status, body = self.request(training_api.recover, pk=self.run.pk)
            self.assertEqual(status, 200, body)
        self.assertEqual(self.keys().count('training.interrupted'), 1)
        self.assertEqual(self.keys().count('training.recovered'), 1)
        self.assertNotIn('training.failed', self.keys())
        self.assertIn('training.created', self.keys())
        self.assertIn('training.queued', self.keys())

    def test_cancel_requests_queued_and_running_and_visibility_cycles(self):
        self.client.force_login(self.user)
        url = '/training/%s/' % self.run.pk
        for _ in range(2):
            self.assertEqual(self.client.post(url, {'action': 'cancel'}).status_code, 302)
        self.assertEqual(self.keys(), ['training.cancel_requested', 'training.cancelled'])
        for action in ('delete', 'delete', 'restore', 'restore', 'delete', 'restore'):
            self.client.post(url, {'action': action})
        self.assertEqual(self.keys().count('training.deleted'), 2)
        self.assertEqual(self.keys().count('training.restored'), 2)
        self.run = self.make_run(); self.claim()
        url = '/training/%s/' % self.run.pk
        self.client.post(url, {'action': 'cancel'}); self.client.post(url, {'action': 'cancel'})
        from OpenBench import training_api
        status, body = self.request(training_api.report, {'state': 'CANCELLED', 'sequence': 1}, pk=self.run.pk)
        self.assertEqual(status, 200, body)
        self.assertEqual(self.keys().count('training.cancel_requested'), 2)
        self.assertEqual(self.keys().count('training.cancelled'), 2)

    def test_validation_initial_queue_and_failure(self):
        from OpenBench.training_tasks import TrainingTasks
        self.run.state = 'VALIDATING'; self.run.save()
        service = TrainingTasks(threading.Event())
        with patch('OpenBench.training_tasks.resolve_inputs', return_value=(self.snapshot, self.dataset)):
            service.validate(); service.validate()
        self.assertEqual(self.keys(), ['training.queued'])
        run = self.make_run(); run.state = 'PREPARING'; run.save()
        service.failed(run, ValidationError('private/path'), 'PREPARING', 'VALIDATING', 'Input validation')
        self.assertEqual(self.keys()[-1], 'training.failed')
        self.assertNotIn('private/path', json.dumps(NotificationDelivery.objects.latest('id').summary))

    def test_continuation_creates_separate_run_and_initial_events(self):
        from OpenBench.training_checkpoints import resume_training
        self.claim(); self.checkpoint(1)
        TrainingRun.objects.filter(pk=self.run.pk).update(state='FAILED')
        self.run.refresh_from_db()
        resumed = resume_training(self.user, self.run)
        self.assertNotEqual(resumed.pk, self.run.pk)
        self.assertEqual(list(NotificationDelivery.objects.filter(subject_key='trainingrun:%s' % resumed.pk).order_by('id').values_list('event_key', flat=True)),
                         ['training.created', 'training.continued', 'training.queued'])


class ConcurrentNotifications(NotificationFixture, TransactionTestCase):
    def parallel(self, operation):
        from concurrent.futures import ThreadPoolExecutor
        from django.db import connection, close_old_connections
        if connection.vendor != 'postgresql':
            self.skipTest('Requires PostgreSQL row locks.')
        barrier = threading.Barrier(2)
        def execute(_):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return operation()
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            return list(pool.map(execute, [0, 1]))

    def test_two_senders_only_one_claim(self):
        self.event()
        results = self.parallel(discord.claim_delivery)
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(NotificationDelivery.objects.get().attempts, 1)

    def test_replayed_event_enqueues_once(self):
        test = self.make_test()
        self.parallel(lambda: record_event('test.passed', test, self.user.pk))
        self.assertEqual(NotificationDelivery.objects.count(), 1)
        self.assertEqual(LifecycleEvent.objects.count(), 1)

    def test_concurrent_controls_only_emit_one_transition(self):
        from OpenBench.workloads.modify_workload import modify_workload
        test = self.make_test()
        self.parallel(lambda: self.post(modify_workload, {}, test.pk, 'STOP'))
        self.assertEqual(NotificationDelivery.objects.filter(event_key='test.stopped').count(), 1)
