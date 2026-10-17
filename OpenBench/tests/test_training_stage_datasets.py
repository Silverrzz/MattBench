import copy
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.contrib.auth.models import User
from django.http import JsonResponse
from django.core.exceptions import ValidationError
from django.test import RequestFactory, TestCase
from django.utils import timezone

from OpenBench.models import EngineConfig, Profile, TrainingDataset, TrainingSchedule, TrainingRun
from OpenBench.schedule_builder import DEFAULT_SPEC, MANIFEST, MANIFEST_VERSION, generate_schedule
from OpenBench.training_views import TrainingForm, new_training
from OpenBench.training import resolve_inputs
from OpenBench.training_datasets import dataset_input, effective_dataset, finish_publication, raw_source_input, source_key
from OpenBench.training_sources import registry_selections


class TrainingStageDatasetTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='stage-datasets')
        Profile.objects.create(user=self.user, enabled=True)
        self.engine = EngineConfig.objects.create(name='Stage test', enabled=True)
        self.ranges = [dict(start=1, end=100), dict(start=101, end=900), dict(start=901, end=1100)]
        spec = copy.deepcopy(DEFAULT_SPEC)
        spec['superbatches'] = 1100
        for channel, value in [('lr', 0.001), ('wdl', 0.2)]:
            spec[channel + '_stages'] = [dict(**bounds, kind='constant', initial=value, final=value) for bounds in self.ranges]
        _, files, settings = generate_schedule(spec)
        self.assertEqual(json.loads(files[MANIFEST])['version'], MANIFEST_VERSION)
        self.schedule = TrainingSchedule.objects.create(owner=self.user, name='Current builder', files=files, settings=settings)
        self.datasets = [TrainingDataset.objects.create(owner=self.user, name='Dataset %d' % i,
                         repo='test/stage-%d' % i, checked=timezone.now()) for i in range(3)]

    def form(self, count):
        return TrainingForm(self.user, data=dict(name='StageTest', engine=str(self.engine.pk),
            schedule=str(self.schedule.pk), checkpoint_retention='all', workload_size=50,
            stage_datasets=json.dumps([dict(stage=i, dataset=str(row.pk)) for i, row in enumerate(self.datasets[:count])])))

    def test_training_page_exposes_every_current_builder_stage(self):
        request = RequestFactory().get('/training/new/', {'schedule': str(self.schedule.pk)})
        request.user = self.user
        # Exercise the real view/queryset and the exact payload consumed by the UI,
        # without unrelated navigation/template configuration.
        with patch('OpenBench.views.render', side_effect=lambda request, template, context: JsonResponse(context['schedule_options'], safe=False)):
            response = new_training(request)
        self.assertEqual(response.status_code, 200)
        row = next(row for row in json.loads(response.content) if row['id'] == str(self.schedule.pk))
        self.assertEqual(row['stages'], self.ranges)

    def test_form_accepts_and_snapshots_one_dataset_per_stage(self):
        form = self.form(3)
        self.assertTrue(form.is_valid(), form.errors.as_json())
        self.assertEqual(form.cleaned_data['dataset_stages'], [dict(**dataset.snapshot(), **bounds)
                         for dataset, bounds in zip(self.datasets, self.ranges)])

    def test_form_rejects_missing_stage_datasets(self):
        for count in (0, 1, 2):
            with self.subTest(count=count):
                form = self.form(count)
                self.assertFalse(form.is_valid())
                self.assertIn('Choose a dataset for every stage.', form.errors['stage_datasets'])

    def test_form_accepts_multiple_datasets_and_preserves_stage_bounds(self):
        form = self.form(3)
        choices = [[self.datasets[0], self.datasets[1]], [self.datasets[1]], self.datasets]
        form.data['stage_datasets'] = json.dumps([{'stage': i, 'datasets': [str(row.pk) for row in rows]} for i, rows in enumerate(choices)])
        self.assertTrue(form.is_valid(), form.errors.as_json())
        stages = form.cleaned_data['dataset_stages']
        self.assertEqual(stages[0], {**self.ranges[0], 'sources': [row.snapshot() for row in choices[0]]})
        self.assertEqual(stages[1], {**self.ranges[1], **self.datasets[1].snapshot()})
        self.assertEqual(registry_selections({'stages': stages}), [[str(row.pk) for row in rows] for rows in choices])

    def test_form_rejects_empty_duplicate_or_unavailable_choices(self):
        self.datasets[2].archived = True
        self.datasets[2].save()
        for ids in ([], [str(self.datasets[0].pk)] * 2, [str(self.datasets[2].pk)], ['unknown'], 'bad', [None]):
            form = self.form(3)
            selections = json.loads(form.data['stage_datasets'])
            selections[1:] = [{'stage': index, 'dataset': str(self.datasets[0].pk)} for index in (1, 2)]
            selections[0] = {'stage': 0, 'datasets': ids}
            form.data['stage_datasets'] = json.dumps(selections)
            self.assertFalse(form.is_valid(), ids)
            self.assertIn('stage_datasets', form.errors)

    def test_custom_schedules_accept_multiple_sources_without_builder_metadata(self):
        self.schedule.files.pop(MANIFEST)
        self.schedule.save()
        form = self.form(1)
        form.data['workload_size'] = 0
        self.assertTrue(form.is_valid(), form.errors.as_json())
        self.assertEqual(form.cleaned_data['dataset_stages'], [])
        form = self.form(1)
        form.data.update(workload_size=0, stage_datasets=json.dumps([{'stage': 0, 'datasets': [str(row.pk) for row in self.datasets]}]))
        self.assertTrue(form.is_valid(), form.errors.as_json())
        self.assertEqual(form.cleaned_data['dataset_stages'], [{'sources': [row.snapshot() for row in self.datasets]}])

    def test_new_run_persists_multiple_sources_without_modifying_saved_schedule(self):
        before = copy.deepcopy(self.schedule.files), copy.deepcopy(self.schedule.settings), self.schedule.version
        form = self.form(3)
        form.data['stage_datasets'] = json.dumps([{'stage': i, 'datasets': [str(row.pk) for row in self.datasets[:2]]} for i in range(3)])
        self.client.force_login(self.user)
        with patch('OpenBench.training_views.hf_token', return_value='token'):
            response = self.client.post('/training/new/', form.data)
        self.assertEqual(response.status_code, 302)
        run = TrainingRun.objects.get(name='StageTest')
        self.assertEqual(registry_selections(run.dataset), [[str(row.pk) for row in self.datasets[:2]]] * 3)
        self.schedule.refresh_from_db()
        self.assertEqual((self.schedule.files, self.schedule.settings, self.schedule.version), before)

    def test_every_supported_existing_builder_manifest_accepts_multiple_datasets(self):
        for version in range(1, MANIFEST_VERSION + 1):
            metadata = json.loads(self.schedule.files[MANIFEST])
            metadata['version'] = version
            self.schedule.files[MANIFEST] = json.dumps(metadata)
            self.schedule.save()
            count = 1 if version == 1 else 3
            form = self.form(count)
            form.data['stage_datasets'] = json.dumps([{'stage': i, 'datasets': [str(row.pk) for row in self.datasets[:2]]} for i in range(count)])
            self.assertTrue(form.is_valid(), (version, form.errors.as_json()))

    def test_new_runs_default_to_uninterrupted_and_preserve_explicit_workload_sizes(self):
        self.client.force_login(self.user)
        for index, (value, expected) in enumerate([(None, 0), ('', 0), ('0', 0), ('50', 50)]):
            with self.subTest(value=value):
                data = self.form(3).data.copy()
                data['name'] = 'WorkloadDefault%d' % index
                if value is None:
                    data.pop('workload_size')
                else:
                    data['workload_size'] = value
                with patch('OpenBench.training_views.hf_token', return_value='token'):
                    response = self.client.post('/training/new/', data)
                self.assertEqual(response.status_code, 302)
                self.assertEqual(TrainingRun.objects.get(name=data['name']).workload_size, expected)

    def test_legacy_runs_preserve_registry_choices_and_preparation(self):
        run = self.resolved_run()
        first = run.dataset['stages'][0]['sources'][0]
        run.dataset['stages'] = [{**self.ranges[0], **first}]
        source = raw_source_input(run, '0')
        run.preparation['0'] = {'source_key': source_key(source), 'dataset': {**source, 'commit': 'e' * 40}}
        effective = effective_dataset(run)
        self.assertNotIn('sources', effective['stages'][0])
        self.assertEqual(effective['stages'][0]['registry_id'], str(self.datasets[0].pk))
        self.assertEqual(effective['stages'][0]['commit'], 'e' * 40)

    def resolved_run(self):
        first, second = [row.snapshot() for row in self.datasets[:2]]
        run = SimpleNamespace(owner_id=self.user.pk, snapshot={'settings': self.schedule.settings}, preparation={},
            dataset={'repo': first['repo'], 'stages': [{**self.ranges[0], 'sources': [first, second]}, {**self.ranges[1], **first}]})
        infos = {first['repo']: SimpleNamespace(sha='a' * 40), second['repo']: SimpleNamespace(sha='b' * 40)}
        api = Mock()
        api.dataset_info.side_effect = lambda repo, **kwargs: infos[repo]
        def metadata(*args):
            files = [{'path': 'data.vf', 'size': 100, 'sha256': 'd' * 64, 'format': 'vf', 'shuffled': True, 'interleaved': True}]
            return {'files': files, 'manifest_present': True, 'manifest': {'version': 1, 'files': files}, 'statistics': {}}
        with patch('OpenBench.training.hf_token', return_value='token'), patch('huggingface_hub.HfApi', return_value=api), patch('OpenBench.dataset_manifest.resolve_dataset', side_effect=metadata), patch('OpenBench.training.requests.get', return_value=Mock(json=Mock(return_value={'sha': 'c' * 40}))):
            run.snapshot, run.dataset = resolve_inputs(run)
        self.assertEqual(api.dataset_info.call_count, 2)
        return run

    def test_resolution_pins_every_source_and_reuses_files_across_stages(self):
        run = self.resolved_run()
        self.assertEqual(len(run.dataset['files']), 2)
        self.assertEqual(run.dataset['size'], 200)
        self.assertEqual([file['commit'] for file in run.dataset['files']], ['a' * 40, 'b' * 40])
        self.assertEqual(run.dataset['stages'][0]['file_indices'], [0, 1])
        self.assertEqual(run.dataset['stages'][1]['file_indices'], [0])
        self.assertEqual(raw_source_input(run, '0.1')['repo'], self.datasets[1].repo)
        self.assertEqual(raw_source_input(run, '1')['repo'], self.datasets[0].repo)
        for selector in (None, '0', '0.2', '-1', '0.-1', '0.0.0', 'no', '2'):
            with self.assertRaises(ValidationError):
                raw_source_input(run, selector)

    def test_publication_updates_one_source_and_reuses_it_in_later_stages(self):
        resolved = self.resolved_run()
        run = TrainingRun.objects.create(owner=self.user, engine=self.engine, name='Publish', dataset=resolved.dataset,
            snapshot=resolved.snapshot, state='CONVERTING')
        for selector, revision in [('0.0', 'e' * 40), ('0.1', 'f' * 40)]:
            source = raw_source_input(run, selector)
            files = [{**source['manifest']['files'][0], 'path': 'prepared.vf'}]
            manifest = {'version': 1, 'files': files}
            plan = {'source_key': source_key(source)}
            self.assertEqual(finish_publication(run, selector, source, plan, manifest, files, {}, revision).status_code, 200)
            run.refresh_from_db()
        self.assertEqual(set(run.preparation), {'0.0', '0.1'})
        effective = effective_dataset(run)
        self.assertEqual(len(effective['files']), 2)
        self.assertEqual([file['commit'] for file in effective['files']], ['e' * 40, 'f' * 40])
        self.assertEqual(effective['stages'][1]['file_indices'], [0])
        self.assertEqual(dataset_input(run, '1')['commit'], 'e' * 40)
        self.assertEqual(registry_selections(effective), registry_selections(run.dataset))
