import json
from unittest.mock import Mock, patch

import requests
from django.contrib.auth.models import User
from django.test import Client, TestCase

from OpenBench.bullet import DEFAULT_BULLET_COMMIT, pinned_bullet_commit
from OpenBench.models import Profile, TrainingConfiguration, TrainingSchedule
from OpenBench.schedule_builder import DEFAULT_SPEC, MANIFEST, SOURCE, builder_state, generate_schedule


class TrainingConfigurationTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user('admin', is_superuser=True)
        self.user = User.objects.create_user('trainer')
        self.staff = User.objects.create_user('staff', is_staff=True)
        for user in (self.admin, self.user, self.staff):
            Profile.objects.create(user=user, enabled=True)
        self.client.force_login(self.admin)
        self.commit = 'a' * 40

    def save(self, **data):
        return self.client.post('/manage/training/', {'version': pinned_bullet_commit(), 'bullet_ref': 'main', **data})

    def response(self, commit=None):
        return Mock(status_code=200, json=Mock(return_value={'sha': commit or self.commit}))

    def test_only_active_superusers_can_read_or_update(self):
        for user in (self.user, self.staff):
            self.client.force_login(user)
            self.assertEqual(self.client.get('/manage/training/').status_code, 403)
            self.assertEqual(self.save().status_code, 403)
        self.client.logout()
        self.assertEqual(self.client.get('/manage/training/').status_code, 302)
        self.assertEqual(self.save().status_code, 302)
        self.assertFalse(TrainingConfiguration.objects.exists())

    def test_csrf_required(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.admin)
        self.assertEqual(client.post('/manage/training/', {'bullet_ref': 'main'}).status_code, 403)

    @patch('OpenBench.training_configuration.requests.get')
    def test_latest_resolves_and_persists_without_changing_saved_schedule(self, get):
        _, files, config = generate_schedule(DEFAULT_SPEC)
        saved = TrainingSchedule.objects.create(name='Saved', owner=self.user, files=files, settings=config)
        get.return_value = self.response()
        self.assertEqual(self.save(action='latest', bullet_ref='').status_code, 302)
        get.assert_called_once_with('https://api.github.com/repos/jw1912/bullet/commits/main', timeout=20)
        self.assertEqual(pinned_bullet_commit(), self.commit)
        saved.refresh_from_db()
        self.assertEqual(saved.settings['bullet_ref'], DEFAULT_BULLET_COMMIT)
        self.client.force_login(self.user)
        self.assertContains(self.client.get('/training/schedules/new/'), self.commit)
        self.assertContains(self.client.get('/training/schedules/builder/'), self.commit[:12])
        result = self.client.post('/training/schedules/builder/', json.dumps({
            'action': 'save', 'spec': DEFAULT_SPEC, 'scope': 'personal', 'name': 'New',
        }), content_type='application/json')
        self.assertEqual(result.status_code, 200)
        new = TrainingSchedule.objects.get(pk=result.json()['id'])
        self.assertEqual(new.settings['bullet_ref'], self.commit)
        self.assertEqual(json.loads(new.files[MANIFEST])['bullet_source'], 'https://github.com/jw1912/bullet/tree/' + self.commit)
        self.assertEqual(builder_state(new)[1], True)
        result = self.client.post('/training/schedules/%s/builder/' % saved.pk, json.dumps({
            'action': 'save', 'spec': DEFAULT_SPEC, 'scope': 'personal', 'name': saved.name, 'version': saved.version,
        }), content_type='application/json')
        self.assertEqual(result.status_code, 200)
        saved.refresh_from_db()
        self.assertEqual(saved.settings['bullet_ref'], DEFAULT_BULLET_COMMIT)
        self.assertTrue(builder_state(saved)[1])

    @patch('OpenBench.training_configuration.requests.get')
    def test_branch_tag_and_sha_resolve_to_fixed_commits(self, get):
        get.return_value = self.response()
        for ref in ('main', 'release/test', self.commit):
            self.assertEqual(self.save(bullet_ref=ref).status_code, 302)
            self.assertEqual(pinned_bullet_commit(), self.commit)
        self.assertIn('release%2Ftest', get.call_args_list[1].args[0])

    @patch('OpenBench.training_configuration.requests.get')
    def test_failed_verification_does_not_change_pin(self, get):
        for response in (Mock(status_code=404), self.response('bad-sha'), Mock(json=Mock(side_effect=ValueError))):
            get.return_value = response
            self.assertEqual(self.save().status_code, 200)
            self.assertEqual(pinned_bullet_commit(), DEFAULT_BULLET_COMMIT)
        get.side_effect = requests.Timeout
        self.assertContains(self.save(), 'Could not verify')
        self.assertFalse(TrainingConfiguration.objects.exists())

    @patch('OpenBench.training_configuration.requests.get')
    def test_stale_update_cannot_overwrite_another_admin(self, get):
        TrainingConfiguration.objects.create(bullet_commit='b' * 40)
        get.return_value = self.response()
        result = self.save(version=DEFAULT_BULLET_COMMIT)
        self.assertContains(result, 'changed in another tab')
        self.assertEqual(pinned_bullet_commit(), 'b' * 40)

    @patch('OpenBench.training_configuration.requests.get')
    def test_invalid_reference_never_contacts_github(self, get):
        self.assertContains(self.save(bullet_ref='../../bad?ref'), 'Enter a valid')
        get.assert_not_called()
