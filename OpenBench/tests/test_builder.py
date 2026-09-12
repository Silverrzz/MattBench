import copy
import json
from types import SimpleNamespace

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase

from OpenBench.builder_values import import_layout, parse_array
from OpenBench.schedule_builder import DEFAULT_SPEC, MANIFEST, SOURCE, builder_state, dataset_stages, generate_schedule, validate_spec


class BuilderTests(SimpleTestCase):
    def spec(self, **changes):
        return {**copy.deepcopy(DEFAULT_SPEC), **changes}

    def test_legacy_probabilities_preserve_piece_indices(self):
        old = self.spec(piece_count_keep=[i / 32 for i in range(2, 33)])
        old.pop('lr_convention')
        old.pop('presentation')
        spec = validate_spec(old)
        self.assertEqual(spec['piece_count_keep'], [0, 0] + old['piece_count_keep'])
        self.assertEqual(spec['lr_convention'], 'legacy')
        self.assertFalse(spec['wdl_filtered'])

    def test_array_import_is_atomic_and_oriented(self):
        text = 'const BUCKETS: [usize; 32] = [\n' + ',\n'.join('/* rank */ ' + ', '.join([str(i)] * 4) for i in range(8)) + ', // end\n];'
        layout = import_layout(text, 'a8')
        self.assertEqual(layout['king_layout'][:8], [7] * 8)
        self.assertEqual(layout['king_layout'][-8:], [0] * 8)
        self.assertEqual(layout['input_buckets'], 8)
        self.assertEqual(import_layout(str(layout['king_layout']))['king_layout'], layout['king_layout'])
        for invalid in ('[1, 2, bad]', '[0, 1] trailing', '[1e999]', '[1] + [2]'):
            with self.assertRaises(ValidationError):
                parse_array(invalid)
        with self.assertRaises(ValidationError):
            import_layout(str([1] * 32))
        asymmetric = [0] * 64
        asymmetric[0] = 1
        with self.assertRaises(ValidationError):
            import_layout(str(asymmetric))
        self.assertFalse(import_layout(str(asymmetric), mirrored=False)['mirrored'])

    def test_target_distribution_and_model_validation(self):
        distribution = [0, 0] + [1 / 31] * 31
        spec = validate_spec(self.spec(piece_count_keep=distribution, piece_count_mode='target', piece_count_sampling=True, wdl_filtered=True))
        self.assertEqual(spec['piece_count_keep'], distribution)
        for changes in ({'piece_count_keep': [0] * 33, 'piece_count_mode': 'target'}, {'wdl_model_params_a': [float('nan')] * 4}, {'mom_target': 0}, {'material_min': 79}, {'wdl_heuristic_scale': 0}, {'wdl_filtered': True, 'wdl_model_params_b': [0, 0, 0, -1]}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                validate_spec(self.spec(**changes))
        # Endpoints positive but the denominator dips below zero at material 1.5.
        with self.assertRaises(ValidationError):
            validate_spec(self.spec(wdl_filtered=True, material_min=1, material_max=2, mom_target=1, wdl_model_params_b=[0, 1, -3, 2.2]))

    def test_no_collapsing_or_silent_boundary_repair(self):
        stages = [{'start': 1, 'end': 100, 'kind': 'constant', 'initial': 0.001, 'final': 0.001}, {'start': 101, 'end': 800, 'kind': 'linear', 'initial': 0.001, 'final': 0.0001}]
        spec = validate_spec(self.spec(lr_stages=stages))
        self.assertEqual([(x['start'], x['end']) for x in spec['lr_stages']], [(1, 100), (101, 800)])
        self.assertEqual(dataset_stages(spec), [{'start': 1, 'end': 100}, {'start': 101, 'end': 800}])
        for change in ({'superbatches': 900}, {'superbatches': 700}, {'lr_stages': [{**stages[0], 'start': 2}, stages[1]]}):
            with self.assertRaises(ValidationError):
                validate_spec({**spec, **change})
        self.assertEqual(stages[1]['start'], 101)

    def test_modes_have_identical_source_and_save_reload(self):
        first, files, settings = generate_schedule(self.spec())
        second, other_files, _ = generate_schedule({**first, 'presentation': {'lr': 'boundaries', 'wdl': 'boundaries'}})
        self.assertEqual(files[SOURCE], other_files[SOURCE])
        self.assertEqual(json.loads(files[MANIFEST])['version'], 4)
        restored, current = builder_state(SimpleNamespace(files=files, settings=settings))
        self.assertTrue(current)
        self.assertEqual(restored, first)
        files[SOURCE] += '\n// manual edit\n'
        _, current = builder_state(SimpleNamespace(files=files, settings=settings))
        self.assertFalse(current)

    def test_all_schedulers_and_union_of_channels(self):
        kinds = ['constant', 'linear', 'cosine', 'exponential', 'step', 'drop']
        stages = [{'start': i + 1, 'end': i + 1, 'kind': kind, 'initial': 0.01, 'final': 0.001, 'gamma': 0.5, 'interval': 1, 'warmup_batches': 4} for i, kind in enumerate(kinds)]
        _, files, _ = generate_schedule(self.spec(superbatches=6, lr_stages=stages, wdl_stages=[{'start': 1, 'end': 6, 'kind': 'linear', 'initial': 0, 'final': 1}]))
        source = files[SOURCE]
        for name in ('ConstantLR', 'LinearDecayLR', 'CosineDecayLR', 'ExponentialDecayLR', 'StepLR', 'DropLR'):
            self.assertIn('lr::' + name, source)
        self.assertEqual(source.count('lr::Sequence {'), 5)
        self.assertIn('lr_schedule.clone().boxed()', source)
        self.assertIn('lr_schedule.lr(step.batch(), superbatch)', source)

    def test_sequence_within_stage_preserves_dataset_ranges_and_reload(self):
        stages = [
            {'start': 1, 'end': 100, 'kind': 'constant', 'initial': 0.01, 'final': 0.01},
            {'start': 101, 'end': 800, 'kind': 'sequence', 'segments': [
                {'start': 1, 'end': 50, 'kind': 'cosine', 'initial': 0.01, 'final': 0.001, 'warmup_batches': 4},
                {'start': 51, 'end': 700, 'kind': 'linear', 'initial': 0.001, 'final': 0.0001},
            ]},
        ]
        spec, files, settings = generate_schedule(self.spec(lr_stages=stages))
        self.assertEqual(dataset_stages(spec), [{'start': 1, 'end': 100}, {'start': 101, 'end': 800}])
        self.assertEqual(files[SOURCE].count('lr::Sequence {'), 2)
        self.assertIn('first_scheduler_final_superbatch: 50', files[SOURCE])
        self.assertIn('first_scheduler_final_superbatch: 100', files[SOURCE])
        self.assertIn('final_superbatch: 650', files[SOURCE])
        restored, current = builder_state(SimpleNamespace(files=files, settings=settings))
        self.assertTrue(current)
        self.assertEqual(restored, spec)
        _, boundaries, _ = generate_schedule({**spec, 'presentation': {'lr': 'boundaries', 'wdl': 'boundaries'}})
        self.assertEqual(files[SOURCE], boundaries[SOURCE])
        # WDL boundaries still create dataset intervals, independently of LR segments.
        spec['wdl_stages'] = [
            {'start': 1, 'end': 200, 'kind': 'constant', 'initial': 0, 'final': 0},
            {'start': 201, 'end': 800, 'kind': 'constant', 'initial': 1, 'final': 1},
        ]
        self.assertEqual(dataset_stages(validate_spec(spec)), [
            {'start': 1, 'end': 100}, {'start': 101, 'end': 200}, {'start': 201, 'end': 800},
        ])

    def test_sequence_validation_never_repairs_segment_ranges(self):
        segment = {'start': 1, 'end': 800, 'kind': 'linear', 'initial': 0.01, 'final': 0.001}
        sequence = {'start': 1, 'end': 800, 'kind': 'sequence', 'segments': [segment]}
        for update in ({'end': 799}, {'end': 801}, {'start': 2}, {'start': 1.0}, {'end': 0},
                       {'initial': float('nan')}, {'warmup_batches': 6105}, {'kind': 'exponential', 'final': 0}):
            with self.subTest(update=update), self.assertRaises(ValidationError):
                validate_spec(self.spec(lr_stages=[{**sequence, 'segments': [{**segment, **update}]}]))
        for segments in ([], [sequence], [segment, segment], [None], 'invalid'):
            with self.subTest(segments=segments), self.assertRaises(ValidationError):
                validate_spec(self.spec(lr_stages=[{**sequence, 'segments': segments}]))
        for update in ({'lr_convention': 'legacy'}, {'wdl_stages': [sequence]}):
            with self.subTest(update=update), self.assertRaises(ValidationError):
                validate_spec(self.spec(lr_stages=[sequence], **update))
        ones = [{**segment, 'start': i, 'end': i} for i in range(1, 65)]
        ones[-1]['end'] = 800
        validate_spec(self.spec(lr_stages=[{**sequence, 'segments': ones}]))
        ones[-1]['end'] = 64
        ones.append({**segment, 'start': 65})
        with self.assertRaisesMessage(ValidationError, 'at most 64'):
            validate_spec(self.spec(lr_stages=[{**sequence, 'segments': ones}]))
        self.assertEqual(segment['end'], 800)
