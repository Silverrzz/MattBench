import copy
import json
from types import SimpleNamespace

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase

from OpenBench.builder_values import import_layout, parse_array
from OpenBench.schedule_builder import DEFAULT_SPEC, MANIFEST, SOURCE, builder_state, dataset_stages, generate_schedule, validate_spec
from OpenBench.training_checkpoints import validate_checkpoint_schedule


class BuilderTests(SimpleTestCase):
    def spec(self, **changes):
        return {**copy.deepcopy(DEFAULT_SPEC), **changes}

    def heimdall(self, **changes):
        return self.spec(**{'layers': [768, 16, 32], 'activation': 'crelu', 'threat_inputs': True,
                            'pairwise_activation': True, 'dual_activation': True, 'export_mode': 'heimdall', **changes})

    def test_export_legacy_is_not_dense_quantization(self):
        old = self.spec(layers=[768, 16, 32])
        old.pop('export_mode')
        old.pop('dense_export')
        spec, files, _ = generate_schedule(old)
        self.assertEqual(spec['export_mode'], 'legacy')
        self.assertEqual(spec['dense_export'], {})
        self.assertIn('SavedFormat::id("l0/w").round().quantise::<i16>(255)', files[SOURCE])
        self.assertIn('SavedFormat::id("score/w").transpose(),', files[SOURCE])
        self.assertNotIn('set_params_for_weight', files[SOURCE])

    def test_custom_dense_export_has_per_tensor_types_scales_layout_and_clips(self):
        from OpenBench.builder_export import DENSE_DEFAULT
        row = {**DENSE_DEFAULT, 'weight_format': 'i8', 'weight_scale': 128,
               'bias_format': 'i32', 'bias_scale': 16384, 'transpose': False}
        _, files, _ = generate_schedule(self.spec(layers=[32, 16], export_mode='custom', dense_export={'l1': row, 'score': row}))
        source = files[SOURCE]
        for name in ('l1', 'score'):
            self.assertIn('SavedFormat::id("%s/w").round().quantise::<i8>(128)' % name, source)
            self.assertIn('SavedFormat::id("%s/b").round().quantise::<i32>(16384)' % name, source)
            self.assertIn('optimiser.set_params_for_weight("%s/w"' % name, source)
            self.assertIn('optimiser.set_params_for_weight("%s/b"' % name, source)
        metadata = json.loads(files[MANIFEST])['export']
        self.assertEqual(metadata['mode'], 'custom')
        self.assertEqual(metadata['dense_weights'], 'per_tensor')
        self.assertEqual(metadata['tensors'][-2]['dtype'], 'i8')
        self.assertFalse(metadata['tensors'][-2]['transposed'])
        self.assertIsNotNone(metadata['tensors'][-2]['training_clip'])

    def test_custom_export_validation(self):
        from OpenBench.builder_export import DENSE_DEFAULT
        for row in ({'weight_format': 'i8'}, {**DENSE_DEFAULT, 'weight_scale': 2},
                    {**DENSE_DEFAULT, 'weight_format': 'i8', 'weight_scale': 32768},
                    {**DENSE_DEFAULT, 'bias_format': 'i32', 'bias_scale': 2147483648},
                    {**DENSE_DEFAULT, 'bias_scale': True}, {**DENSE_DEFAULT, 'transpose': 1},
                    {**DENSE_DEFAULT, 'weight_format': 'rust injection'}):
            with self.subTest(row=row), self.assertRaises(ValidationError):
                validate_spec(self.spec(export_mode='custom', dense_export={'score': row}))
        for changes in ({'export_mode': 'bad'}, {'dense_export': []}, {'dense_export': {'l0': DENSE_DEFAULT}}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                validate_spec(self.spec(**changes))

    def test_heimdall_export_matches_reference_types_scales_order_and_clipping(self):
        _, files, _ = generate_schedule(self.heimdall())
        source = files[SOURCE]
        self.assertIn('l0_psqt.matmul(stm) + l0_ti.forward(stm_ti)', source)
        self.assertIn('l0_psqt.matmul(ntm) + l0_ti.forward(ntm_ti)', source)
        self.assertIn('const PSQ_FEATURES: usize = 768;', source)
        self.assertIn('stm_ti[ti_count] = (us - PSQ_FEATURES)', source)
        self.assertIn('if psq_count < 32 { stm[psq_count] = -1; ntm[psq_count] = -1; }', source)
        self.assertNotIn('builder.new_affine("l0/"', source)
        self.assertIn('min_weight: -(127.0 / 255.0)', source)
        self.assertIn('(127.0 / 128.0) * (255.0 / 256.0) * (255.0 / 256.0)', source)
        self.assertIn('f / ((255.0f32 / 256.0) * (255.0 / 256.0))', source)
        tensors = json.loads(files[MANIFEST])['export']['tensors']
        self.assertEqual([(t['tensor'], t['dtype'], t['scale'], t['transposed']) for t in tensors], [
            ('l0/psqt', 'i16', 255, False), ('l0/ti/w', 'i8', 255, False), ('l0/ti/b', 'i16', 255, False),
            ('l1/w', 'i8', 128, False), ('l1/b', 'i32', 16384, False),
            ('l2/w', 'i32', 64, False), ('l2/b', 'i32', 262144, False),
            ('score/w', 'i32', 64, False), ('score/b', 'i32', 16777216, False)])

    def test_heimdall_profile_rejects_incompatible_topology(self):
        for changes in ({'layers': [768]}, {'layers': [768, 32, 32]}, {'layers': [64, 16, 32]},
                        {'dual_layers': [3]}, {'threat_inputs': False}, {'pawn_pair_inputs': True},
                        {'score_buckets': 4}, {'wdl_outputs': True}, {'feature_format': 'f32'},
                        {'hidden_layers_bucketed': False}, {'pairwise_left_activation': 'relu'},
                        {'merged_king_planes': True}, {'half_move_clock': True}, {'mirrored': False}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                validate_spec({**self.heimdall(), **changes})

    def test_checkpoint_export_changes_rejected_but_lr_changes_allowed(self):
        for spec in (self.heimdall(), self.spec(export_mode='custom')):
            normalized, files, config = generate_schedule(spec)
            checkpoint = SimpleNamespace(superbatch=10, run=SimpleNamespace(engine_id=1, snapshot={'files': files, 'settings': config}))
            _, changed, settings = generate_schedule({**normalized, 'export_mode': 'legacy'})
            with self.assertRaisesMessage(ValidationError, 'same export and training-clipping'):
                validate_checkpoint_schedule(checkpoint, SimpleNamespace(pk=1), changed, settings)
            normalized['lr_stages'][0]['initial'] = 0.005
            _, changed, settings = generate_schedule(normalized)
            validate_checkpoint_schedule(checkpoint, SimpleNamespace(pk=1), changed, settings)

    def test_checkpoint_rejects_custom_quantization_scale_and_feature_format_changes(self):
        from OpenBench.builder_export import DENSE_DEFAULT
        spec, files, config = generate_schedule(self.spec(export_mode='custom'))
        checkpoint = SimpleNamespace(superbatch=10, run=SimpleNamespace(engine_id=1, snapshot={'files': files, 'settings': config}))
        for changes in ({'dense_export': {'score': {**DENSE_DEFAULT, 'weight_format': 'i8', 'weight_scale': 128}}},
                        {'feature_format': 'f32'}):
            _, changed, settings = generate_schedule({**spec, **changes})
            with self.assertRaisesMessage(ValidationError, 'same export and training-clipping'):
                validate_checkpoint_schedule(checkpoint, SimpleNamespace(pk=1), changed, settings)

    def test_output_bucket_selection_precedes_hidden_activation(self):
        _, files, _ = generate_schedule(self.spec(layers=[512, 16, 32], pairwise_activation=True,
                                                dual_activation=True, activation='crelu'))
        source = files[SOURCE]
        self.assertIn('builder.new_affine("l1/", 512, 128)', source)
        self.assertIn('builder.new_affine("l2/", 32, 256)', source)
        self.assertIn('builder.new_affine("score/", 32, 8)', source)
        self.assertIn('let preactivation = l1.forward(hidden).select(score_buckets);\n'
                      '        let hidden = preactivation.concat(preactivation.abs_pow(2.0)).crelu();', source)
        self.assertIn('let preactivation = l2.forward(hidden).select(score_buckets);', source)
        metadata = json.loads(files[MANIFEST])['export']
        self.assertTrue(metadata['hidden_layers_bucketed'])
        self.assertEqual(metadata['hidden_bucket_count'], 8)
        self.assertEqual(metadata['hidden_bucket_head'], 'score')
        self.assertEqual(metadata['dual']['layers'], [2])

    def test_wdl_buckets_and_independent_auxiliary_heads(self):
        _, files, _ = generate_schedule(self.spec(layers=[32, 8], score_outputs=False, wdl_outputs=True,
                                                wdl_buckets=4, uncertainty_outputs=True, uncertainty_buckets=2))
        source = files[SOURCE]
        self.assertIn('builder.new_affine("l1/", 64, 32)', source)
        self.assertIn('l1.forward(hidden).select(wdl_buckets)', source)
        self.assertIn('builder.new_affine("wdl/", 8, 12)', source)
        self.assertIn('builder.new_affine("uncertainty/", 8, 2)', source)
        self.assertEqual(json.loads(files[MANIFEST])['export']['hidden_bucket_head'], 'wdl')

    def test_dual_layer_widths_and_skip_connection(self):
        for dual_layers, l2_inputs, head_inputs in (([2], 16, 16), ([3], 8, 32), ([2, 3], 16, 32)):
            with self.subTest(dual_layers=dual_layers):
                _, files, _ = generate_schedule(self.spec(layers=[32, 8, 16], dual_activation=True, dual_layers=dual_layers))
                self.assertIn('builder.new_affine("l2/", %d, 128)' % l2_inputs, files[SOURCE])
                self.assertIn('builder.new_affine("score/", %d, 8)' % head_inputs, files[SOURCE])
                self.assertEqual(files[SOURCE].count('preactivation.concat(preactivation.abs_pow(2.0)).crelu()'), len(dual_layers))
        validate_spec(self.spec(layers=[32, 8, 16], dual_activation=True, skip_connection=True))
        validate_spec(self.spec(layers=[32, 16, 8], dual_activation=True, dual_layers=[3], skip_connection=True))
        for changes in ({'layers': [32]}, {'dual_layers': [1]}, {'dual_layers': [2, 2]}, {'dual_layers': [True]},
                        {'dual_layers': []}, {'dual_layers': [4]}, {'dual_activation': 1},
                        {'pairwise_activation': True, 'pairwise_layers': [2]}, {'skip_connection': True}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                validate_spec(self.spec(**{'layers': [32, 8, 8], 'dual_activation': True, **changes}))

    def test_old_schedules_preserve_shared_layers_and_checkpoint_compatibility(self):
        old = self.spec(layers=[32, 8, 8])
        for key in ('hidden_layers_bucketed', 'dual_activation', 'dual_layers'):
            old.pop(key)
        spec, files, config = generate_schedule(old)
        self.assertFalse(spec['hidden_layers_bucketed'])
        self.assertFalse(spec['dual_activation'])
        self.assertIn('let preactivation = l1.forward(hidden);', files[SOURCE])
        manifest = json.loads(files[MANIFEST])
        manifest['spec'] = old
        files[MANIFEST] = json.dumps(manifest)
        restored, current = builder_state(SimpleNamespace(files=files, settings=config))
        self.assertTrue(current)
        self.assertEqual(restored, spec)
        checkpoint = SimpleNamespace(superbatch=10, run=SimpleNamespace(engine_id=1, snapshot={'files': files, 'settings': config}))
        engine = SimpleNamespace(pk=1)
        _, regenerated, updated = generate_schedule(restored)
        validate_checkpoint_schedule(checkpoint, engine, regenerated, updated)
        for changes in ({'hidden_layers_bucketed': True}, {'dual_activation': True}):
            _, changed_files, changed_config = generate_schedule({**restored, **changes})
            with self.assertRaises(ValidationError):
                validate_checkpoint_schedule(checkpoint, engine, changed_files, changed_config)

    def test_dual_checkpoint_rejects_changed_layers_but_accepts_lr_changes(self):
        spec, files, config = generate_schedule(self.spec(layers=[32, 8, 8], dual_activation=True))
        checkpoint = SimpleNamespace(superbatch=10, run=SimpleNamespace(engine_id=1, snapshot={'files': files, 'settings': config}))
        _, changed_files, changed_config = generate_schedule({**spec, 'dual_layers': [3]})
        with self.assertRaisesMessage(ValidationError, 'same dual activation layers'):
            validate_checkpoint_schedule(checkpoint, SimpleNamespace(pk=1), changed_files, changed_config)
        spec['lr_stages'][0]['initial'] = 0.005
        _, changed_files, changed_config = generate_schedule(spec)
        validate_checkpoint_schedule(checkpoint, SimpleNamespace(pk=1), changed_files, changed_config)

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
        self.assertEqual(json.loads(files[MANIFEST])['version'], 5)
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
