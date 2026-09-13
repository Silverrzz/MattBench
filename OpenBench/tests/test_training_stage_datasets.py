import copy
import json
from unittest.mock import patch

from django.contrib.auth.models import User
from django.http import JsonResponse
from django.test import RequestFactory, TestCase
from django.utils import timezone

from OpenBench.models import EngineConfig, Profile, TrainingDataset, TrainingSchedule
from OpenBench.schedule_builder import DEFAULT_SPEC, MANIFEST, MANIFEST_VERSION, generate_schedule
from OpenBench.training_views import TrainingForm, new_training


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
