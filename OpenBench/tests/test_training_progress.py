from django.template.loader import render_to_string
from django.test import SimpleTestCase


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
                self.assertEqual(self.render(state, progress=50, phase_progress=0),
                                 'Progress: 0.0%% (%s)\nOverall training: 50.0%%' % label)
                for metrics in [{'progress': 12.5}, {'progress': 12.5, 'phase_progress': None}, {}]:
                    self.assertEqual(self.render(state, **metrics),
                                     'Progress: %.1f%% (%s)' % (metrics.get('progress', 0), label))

    def test_other_states_ignore_stale_phase_progress(self):
        for state in ['TRAINING', 'QUEUED', 'FAILED', 'CANCELLED', 'COMPLETED']:
            with self.subTest(state=state):
                self.assertEqual(self.render(state, progress=50, phase_progress=99),
                                 'Progress: 100.0%' if state == 'COMPLETED' else 'Progress: 50.0%')
        self.assertEqual(self.render('TRAINING', progress=50, phase_progress=99, stage=2, stage_count=3),
                         'Progress: 50.0% (Stage 2 of 3)')
