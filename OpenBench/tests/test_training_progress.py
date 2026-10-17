from django.template.loader import render_to_string
from django.test import SimpleTestCase, TestCase

from OpenBench import training_api
from OpenBench.models import TrainingRun
from OpenBench.tests.test_training_workloads import WorkloadFixture


class TrainingProgressTests(SimpleTestCase):
    def render(self, state, **metrics):
        return render_to_string('OpenBench/Blocks/training_progress.html',
                                {'run': {'state': state, 'metrics': metrics}}).strip()

    def test_phase_progress_and_overall_are_separate(self):
        self.assertEqual(self.render('DOWNLOADING', progress=5.20833, phase_progress=28.2037),
                         'Progress: 28.2% (Downloading)\nOverall training: 5.2%')
        for state, label in [('DOWNLOADING', 'Downloading'), ('CONVERTING', 'Converting'),
                             ('COMPILING', 'Compiling'), ('SAVING', 'Uploading')]:
            with self.subTest(state=state):
                self.assertEqual(self.render(state, progress=50, phase_progress=0, progress_known=1),
                                 'Progress: 0.0%% (%s)\nOverall training: 50.0%%' % label)
                for metrics in [{'progress': 12.5}, {'progress': 12.5, 'phase_progress': None}, {}]:
                    self.assertEqual(self.render(state, progress_known=1, **metrics),
                                     'Progress: %.1f%% (%s)' % (metrics.get('progress', 0), label))

    def test_other_states_ignore_stale_phase_progress(self):
        for state in ['TRAINING', 'QUEUED', 'FAILED', 'CANCELLED', 'COMPLETED']:
            with self.subTest(state=state):
                self.assertEqual(self.render(state, progress=50, phase_progress=99),
                                 'Progress: 100.0%' if state == 'COMPLETED' else 'Progress: 50.0%')
        self.assertEqual(self.render('TRAINING', progress=50, phase_progress=99, stage=2, stage_count=3),
                         'Progress: 50.0% (Stage 2 of 3)')

    def test_preparation_steps_and_unknown_progress(self):
        for metrics in [{'progress': 100}, {'progress': 50, 'phase_progress': 100}, {}]:
            expected = 'Progress: In progress (Converting)'
            if 'phase_progress' in metrics:
                expected += '\nOverall training: 50.0%'
            self.assertEqual(self.render('CONVERTING', **metrics), expected)
        self.assertEqual(self.render('CONVERTING', progress=100, progress_known=0, preparation_step='Analysing dataset'),
                         'Progress: In progress (Analysing dataset)')
        self.assertEqual(self.render('CONVERTING', progress=50, phase_progress=100, progress_known=0,
                                     preparation_step='Publishing dataset'),
                         'Progress: In progress (Publishing dataset)\nOverall training: 50.0%')
        self.assertEqual(self.render('CONVERTING', progress=25, progress_known=1, preparation_step='Converting files'),
                         'Progress: 25.0% (Converting files)')
        self.assertEqual(self.render('TRAINING', progress=50, progress_known=0, preparation_step='Analysing dataset'),
                         'Progress: 50.0%')

    def test_list_matches_detail_for_unknown_preparation_progress(self):
        run = {'state': 'CONVERTING', 'get_state_display': 'Converting',
               'metrics': {'progress': 100, 'phase_progress': 100, 'progress_known': 0,
                           'preparation_step': 'Shuffling and interleaving'}}
        rendered = render_to_string('OpenBench/Blocks/training_rows.html', {'runs': [run]})
        self.assertIn('Shuffling and interleaving · In progress', rendered)
        self.assertNotIn('Shuffling and interleaving · 100.0%', rendered)
        self.assertIn('Overall training · 100.0%', rendered)
        run['metrics'].update(progress_known=1, phase_progress=25, preparation_step='Converting files')
        self.assertIn('Converting files · 25.0%', render_to_string('OpenBench/Blocks/training_rows.html', {'runs': [run]}))
        run['metrics'] = {'progress': 100}
        self.assertIn('Converting · In progress', render_to_string('OpenBench/Blocks/training_rows.html', {'runs': [run]}))


class TrainingProgressAPITests(WorkloadFixture, TestCase):
    def check_preparation_reports(self, size):
        self.run.workload_size = size
        self.run.save(update_fields=['workload_size'])
        self.assertEqual(self.claim()['run']['id'], self.run.pk)
        TrainingRun.objects.filter(pk=self.run.pk).update(state='CONVERTING')
        metrics = {'progress': 100, 'progress_known': 0, 'preparation_step': 'Analysing dataset'}
        status, result = self.request(training_api.report, {'state': 'CONVERTING', 'sequence': 1, 'metrics': metrics}, pk=self.run.pk)
        self.assertEqual(status, 200, result)
        self.run.refresh_from_db()
        self.assertEqual(self.run.metrics['preparation_step'], 'Analysing dataset')
        self.assertEqual(self.run.metrics['progress_known'], 0)
        text = render_to_string('OpenBench/Blocks/training_progress.html', {'run': self.run})
        self.assertIn('Progress: In progress (Analysing dataset)', text)
        if size:
            self.assertIn('Overall training: 0.0%', text)
        status, result = self.request(training_api.report, {'state': 'COMPILING', 'sequence': 2,
            'metrics': {'progress': 0, 'progress_known': 1, 'preparation_step': ''}}, pk=self.run.pk)
        self.assertEqual(status, 200, result)
        self.run.refresh_from_db()
        self.assertIn('Progress: 0.0% (Compiling)', render_to_string('OpenBench/Blocks/training_progress.html', {'run': self.run}))

    def test_uninterrupted_run_preparation_reports(self):
        self.check_preparation_reports(0)

    def test_split_run_preparation_reports(self):
        self.check_preparation_reports(2)
