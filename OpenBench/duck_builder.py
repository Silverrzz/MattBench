import json
from pathlib import Path
from string import Template

from django.core.exceptions import ValidationError


def generate(spec, profile):
    from OpenBench.schedule_builder import MANIFEST, MANIFEST_VERSION, SOURCE, assets, dataset_stages, fingerprint, lr_scheduler
    from OpenBench.training import DEFAULT_SETTINGS, validate_schedule
    from OpenBench.training_profiles import profile_settings
    if len(spec['layers']) != 3 or spec['layers'][0] % 4:
        raise ValidationError('The Wakwak export requires three hidden layers and L1 divisible by four.')
    if spec['optimizer'] != 'adamw' or not spec['psqt_inputs'] or spec['input_buckets'] != 1:
        raise ValidationError('This DuckChess profile requires AdamW and one bucket of piece inputs.')
    if any(spec[key] for key in ('threat_inputs', 'pawn_pair_inputs', 'half_move_clock', 'merged_king_planes', 'wdl_outputs', 'uncertainty_outputs', 'skip_connection', 'piece_count_sampling', 'result_filtering', 'wdl_filtered')):
        raise ValidationError('This feature is not supported by the DuckChess profile.')
    if not spec['score_outputs'] or spec['score_buckets'] != 1 or not spec['pairwise_activation'] or spec['pairwise_layers'] != [1] or not spec['dual_activation'] or spec['dual_layers'] != [2]:
        raise ValidationError('Use the Wakwak graph: pairwise L1, dual L2, and one score output.')
    if spec['position_filtering']:
        raise ValidationError('Standard-chess position filters are unavailable for DuckChess. Use quiet-move filtering instead.')
    if spec['loader_interleave'] or spec['random_fen_skip']:
        raise ValidationError('The Wakformat loader concatenates source files and uses its own position shuffle buffer. Use dataset preparation for game ordering.')
    if spec['activation'] != 'crelu' or spec['pairwise_left_activation'] != 'crelu' or spec['pairwise_right_activation'] != 'crelu':
        raise ValidationError('The Wakwak export uses CReLU activations.')
    if spec['export_mode'] != 'legacy' or spec['feature_format'] != 'i16' or spec['split_features']:
        raise ValidationError('The DuckChess profile uses the Wakwak integer export format.')
    text = Template((Path(__file__).parent / 'data/duck_main.rs').read_text()).substitute(
        l1=spec['layers'][0], l2=spec['layers'][1], l3=spec['layers'][2], scale=repr(spec['eval_scale']),
        duck=str(spec['duck_inputs']).lower(), mirrored=str(spec['mirrored']).lower(),
        superbatches=spec['superbatches'], dataset_ranges=', '.join('(%d, %d)' % (row['start'], row['end']) for row in dataset_stages(spec)),
        buffer=spec['buffer_mb'], threads=spec['threads'], quiet=str(spec['filter_tactical']).lower(), max_eval=spec['max_eval'],
        batch_size=spec['batch_size'], batches=spec['batches_per_superbatch'], save_every=spec['save_every'],
        lr_scheduler=lr_scheduler(spec), wdl_rows=', '.join('(%d, %d, %s, %s, %d)' % (stage['start'], stage['end'], repr(float(stage['initial'])), repr(float(stage['final'])), ('constant', 'linear', 'cosine').index(stage['kind'])) for stage in spec['wdl_stages']),
    )
    files = {SOURCE: text, 'LICENSE.txt': assets()[3]}
    settings = {**DEFAULT_SETTINGS, **profile_settings(profile), 'backend': spec['backend'], 'threads': spec['threads'], 'resume_supported': True,
                'build': ['cargo', 'build', '--release', '--manifest-path', 'crates/bullet_lib/Cargo.toml', '--example', 'mattbench', '--features', spec['backend']],
                'shuffle_seed': spec['seed'], **{key: spec[key] for key in ('shuffle', 'interleave', 'shuffle_memory_mb', 'interleave_fan_in', 'dataset_shard_mb')}}
    features = 768 + 64 * spec['duck_inputs']
    l1, l2, l3 = spec['layers']
    size = (features * l1 + l1) * 2 + l1 * l2 + l2 * 4 + 2 * l2 * l3 * 4 + l3 * 4 + l3 * 4 + 4
    files[MANIFEST] = json.dumps({'version': MANIFEST_VERSION, 'spec': spec, 'training_profile': profile,
        'fingerprint': fingerprint(files, settings), 'workload_bounds': True,
        'export': {'mode': 'wakwak', 'input_features': features, 'duck_inputs': spec['duck_inputs'], 'layers': spec['layers'],
                   'mirrored': spec['mirrored'], 'byte_order': 'little', 'network_bytes': (size + 63) // 64 * 64,
                   'layout': 'wakwak-pairwise-dual-v1', 'quantisation': [255, 128, 64], 'padding': 64}}, indent=2) + '\n'
    return spec, files, validate_schedule(files, settings)
