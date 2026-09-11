import hashlib
import json
import math
from functools import lru_cache
from pathlib import Path
from string import Template
from textwrap import indent

from django.core.exceptions import ValidationError

from OpenBench.training import DEFAULT_SETTINGS, validate_schedule


BULLET_COMMIT = '629ee50000b2afb7b3337595401c830d3b1e0f42'
MANIFEST = 'mattbench-builder.json'
SOURCE = 'examples/mattbench.rs'
DEFAULT_SPEC = {
    'layers': [768], 'activation': 'screlu', 'psqt_inputs': True, 'threat_inputs': False, 'pawn_pair_inputs': False,
    'input_buckets': 1, 'mirrored': True, 'king_layout': [0] * 64,
    'score_outputs': True, 'score_buckets': 8, 'wdl_outputs': False, 'wdl_buckets': 1,
    'uncertainty_outputs': False, 'uncertainty_buckets': 1,
    'half_move_clock': False, 'merged_king_planes': False, 'skip_connection': False, 'pairwise_activation': False,
    'random_fen_skip': 0.0, 'position_filtering': True,
    'min_ply': 16, 'max_ply': 100000, 'min_eval': 0, 'max_eval': 31338,
    'min_pieces': 4, 'max_pieces': 32, 'filter_tactical': True, 'filter_check': True, 'filter_castling': False,
    'piece_count_sampling': False, 'piece_count_keep': [1.0] * 31,
    'batch_size': 16384, 'batches_per_superbatch': 6104, 'superbatches': 800, 'save_every': 1,
    'lr_stages': [{'start': 1, 'end': 800, 'kind': 'cosine', 'initial': 0.001, 'final': 0.00001}],
    'wdl_stages': [{'start': 1, 'end': 800, 'kind': 'constant', 'initial': 0.75, 'final': 0.75}],
    'eval_scale': 400.0,
    'threads': 4, 'buffer_mb': 1024, 'seed': 42, 'backend': 'cuda', 'feature_format': 'i16',
    'shuffle': True, 'interleave': True, 'shuffle_memory_mb': 256,
    'interleave_fan_in': 32, 'dataset_shard_mb': 512,
}


def validate_spec(value):
    if isinstance(value, dict):
        value = {'psqt_inputs': True, **value}
        value = {**{key: DEFAULT_SPEC[key] for key in ('random_fen_skip', 'position_filtering', 'min_ply', 'max_ply', 'min_eval', 'max_eval', 'min_pieces', 'max_pieces', 'filter_tactical', 'filter_check', 'filter_castling', 'piece_count_sampling', 'piece_count_keep')}, **value}
        if 'output_buckets' in value:
            buckets = value.pop('output_buckets')
            if value.pop('output_buckets_enabled', True) is False:
                buckets = 1
            value = {'score_outputs': not value.get('wdl_outputs', False), 'score_buckets': buckets,
                     'wdl_buckets': buckets, 'uncertainty_buckets': buckets, **value}
        value = {**{key: False for key in ('wdl_outputs', 'uncertainty_outputs', 'half_move_clock', 'merged_king_planes', 'skip_connection', 'pairwise_activation')}, **value}
        value = {**{key: DEFAULT_SPEC[key] for key in ('shuffle', 'interleave', 'shuffle_memory_mb', 'interleave_fan_in', 'dataset_shard_mb')}, **value}
    if isinstance(value, dict) and 'lr_kind' in value:
        value = dict(value)
        for channel in ('lr', 'wdl'):
            initial = value.pop(channel + '_start')
            final = value.pop(channel + '_end')
            kind = value.pop('lr_kind') if channel == 'lr' else ('constant' if initial == final else 'linear')
            value[channel + '_stages'] = [{'start': 1, 'end': value['superbatches'], 'kind': kind, 'initial': initial, 'final': final}]
    if not isinstance(value, dict) or set(value) != set(DEFAULT_SPEC):
        raise ValidationError('The builder configuration is incomplete. Reload the builder.')
    spec = json.loads(json.dumps(value))
    for key, minimum, maximum in (
        ('input_buckets', 1, 64), ('score_buckets', 1, 32), ('wdl_buckets', 1, 32), ('uncertainty_buckets', 1, 32), ('batch_size', 1, 1048576),
        ('batches_per_superbatch', 1, 1000000), ('superbatches', 1, 1000000), ('save_every', 1, 1000000),
        ('threads', 1, 255), ('buffer_mb', 16, 65536), ('seed', 0, 2 ** 53 - 1),
        ('shuffle_memory_mb', 16, 65536), ('interleave_fan_in', 2, 256),
        ('dataset_shard_mb', 4, 16384),
        ('min_ply', 0, 100000), ('max_ply', 0, 100000), ('min_eval', 0, 32768), ('max_eval', 0, 32768),
        ('min_pieces', 2, 32), ('max_pieces', 2, 32),
    ):
        if type(spec[key]) is not int or not minimum <= spec[key] <= maximum:
            raise ValidationError('%s must be between %d and %d.' % (key.replace('_', ' ').capitalize(), minimum, maximum))
    for key in ('mirrored', 'psqt_inputs', 'threat_inputs', 'pawn_pair_inputs', 'shuffle', 'interleave',
                'score_outputs', 'wdl_outputs', 'uncertainty_outputs', 'half_move_clock', 'merged_king_planes', 'skip_connection', 'pairwise_activation',
                'position_filtering', 'filter_tactical', 'filter_check', 'filter_castling', 'piece_count_sampling'):
        if type(spec[key]) is not bool:
            raise ValidationError('Invalid %s selection.' % key.replace('_', ' '))
    if not spec['score_outputs'] and not spec['wdl_outputs']:
        raise ValidationError('Enable a score or WDL output. The uncertainty head needs a prediction error to learn from.')
    if not any(spec[key] for key in ('psqt_inputs', 'threat_inputs', 'pawn_pair_inputs', 'half_move_clock')):
        raise ValidationError('Enable at least one input feature.')
    if not spec['psqt_inputs'] and not spec['half_move_clock']:
        spec['input_buckets'] = 1
        spec['king_layout'] = [0] * 64
    for kind in ('ply', 'eval', 'pieces'):
        if spec['min_' + kind] > spec['max_' + kind]:
            raise ValidationError('Minimum %s cannot exceed maximum %s.' % (kind, kind))
    if type(spec['random_fen_skip']) not in (int, float) or not math.isfinite(spec['random_fen_skip']) or not 0 <= spec['random_fen_skip'] <= 1:
        raise ValidationError('Random FEN skip probability must be between 0 and 1.')
    spec['random_fen_skip'] = float(spec['random_fen_skip'])
    keep = spec['piece_count_keep']
    if not isinstance(keep, list) or len(keep) != 31 or any(type(p) not in (int, float) or not math.isfinite(p) or not 0 <= p <= 1 for p in keep):
        raise ValidationError('Set a keep probability between 0 and 1 for each piece count from 2 to 32.')
    spec['piece_count_keep'] = [float(p) for p in keep]
    low, high = (spec['min_pieces'], spec['max_pieces']) if spec['position_filtering'] else (2, 32)
    if spec['piece_count_sampling'] and not any(keep[low - 2:high - 1]):
        raise ValidationError('Keep at least one piece count in the permitted range.')
    for key, options in (
        ('activation', ('screlu', 'crelu')),
        ('backend', ('cuda', 'rocm')), ('feature_format', ('i16', 'f32')),
    ):
        if spec[key] not in options:
            raise ValidationError('Choose a valid %s.' % key.replace('_', ' '))
    for key, minimum, maximum in (
        ('eval_scale', 1.0, 100000.0),
    ):
        if type(spec[key]) not in (float, int) or not minimum <= spec[key] <= maximum or not math.isfinite(spec[key]):
            raise ValidationError('%s must be between %g and %g.' % (key.replace('_', ' ').capitalize(), minimum, maximum))
        spec[key] = float(spec[key])
    layers = spec['layers']
    if not isinstance(layers, list) or not 1 <= len(layers) <= 8 or any(type(size) is not int or not 1 <= size <= 8192 for size in layers):
        raise ValidationError('Use 1–8 hidden layers, with 1–8192 neurons each.')
    if spec['skip_connection'] and (len(layers) < 3 or layers[1] != layers[2]):
        raise ValidationError('The skip connection requires at least three hidden layers, with Layer 2 and Layer 3 the same size.')
    if spec['pairwise_activation'] and layers[0] % 2:
        raise ValidationError('Pairwise activation requires an even feature-layer size.')
    layout = spec['king_layout']
    if not isinstance(layout, list) or len(layout) != 64 or any(type(bucket) is not int or not 0 <= bucket < spec['input_buckets'] for bucket in layout):
        raise ValidationError('Assign all 64 king squares to a valid input bucket.')
    if set(layout) != set(range(spec['input_buckets'])):
        raise ValidationError('Assign at least one king square to each input bucket.')
    if spec['mirrored'] and any(layout[rank * 8 + file] != layout[rank * 8 + 7 - file] for rank in range(8) for file in range(4)):
        raise ValidationError('Mirrored layouts must assign matching files to the same bucket.')
    if spec['save_every'] > spec['superbatches']:
        raise ValidationError('Save frequency cannot exceed the total superbatches.')
    for channel in ('lr', 'wdl'):
        stages = spec[channel + '_stages']
        if not isinstance(stages, list) or not stages:
            raise ValidationError('Add at least one %s stage.' % channel.upper())
        if channel == 'lr' and all(isinstance(stage, dict) and set(stage) == {'start', 'end', 'kind', 'initial', 'final'} for stage in stages):
            stages = [{**stages[0], 'start': 1, 'end': spec['superbatches'], 'final': stages[-1]['final']}]
            spec['lr_stages'] = stages
        next_start = 1
        for index, stage in enumerate(stages, 1):
            label = '%s stage %d' % (channel.upper(), index)
            if not isinstance(stage, dict) or set(stage) != {'start', 'end', 'kind', 'initial', 'final'}:
                raise ValidationError('%s is incomplete.' % label)
            stage['start'] = next_start
            if index == len(stages):
                stage['end'] = spec['superbatches']
            if type(stage['start']) is not int or type(stage['end']) is not int or stage['start'] != next_start or not stage['start'] <= stage['end'] <= spec['superbatches']:
                raise ValidationError('%s must start at SB %d and end within the schedule, without gaps or overlaps.' % (label, next_start))
            if stage['kind'] not in ('constant', 'linear', 'cosine'):
                raise ValidationError('%s has an invalid curve.' % label)
            for key in ('initial', 'final'):
                if type(stage[key]) not in (int, float) or not math.isfinite(stage[key]) or not 0 <= stage[key] <= 1:
                    raise ValidationError('%s values must be between 0 and 1.' % label)
                stage[key] = float(stage[key])
            if stage['kind'] == 'constant' or stage['start'] == stage['end']:
                stage['final'] = stage['initial']
            next_start = stage['end'] + 1
        if next_start != spec['superbatches'] + 1:
            raise ValidationError('%s stages must cover all superbatches.' % channel.upper())
    return spec


@lru_cache(maxsize=1)
def assets():
    root = Path(__file__).parent / 'data'
    library = json.loads((root / 'bullet_schedules.json').read_text(encoding='utf-8'))
    return (
        Template((root / 'builder_main.rs').read_text(encoding='utf-8')),
        (root / 'builder_inputs.rs').read_text(encoding='utf-8'),
        library[0]['files']['examples/mattbench.rs'], library[0]['files']['LICENSE.txt'],
    )


def fingerprint(files, settings):
    data = {'files': {name: source for name, source in files.items() if name != MANIFEST}, 'settings': settings}
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def builder_state(schedule):
    try:
        metadata = json.loads(schedule.files[MANIFEST])
        if metadata['version'] not in (1, 2, 3):
            return None, False
        spec = validate_spec(metadata['spec'])
        return spec, metadata['fingerprint'] == fingerprint(schedule.files, schedule.settings)
    except (KeyError, TypeError, ValueError, ValidationError):
        return None, False


def generate_schedule(value):
    spec = validate_spec(value)
    template, auxiliary, adapter, licence = assets()
    bucket_type = 'ChessBucketsMirrored' if spec['mirrored'] else 'ChessBuckets'
    king_width = 4 if spec['mirrored'] else 8
    rows = ['    ' + ', '.join(str(bucket) for bucket in spec['king_layout'][rank * 8:rank * 8 + king_width]) + ',' for rank in range(8)]
    feature_expression = '%s::new(KING_BUCKETS)' % bucket_type
    auxiliary_module = ''
    if not spec['psqt_inputs'] or any(spec[key] for key in ('threat_inputs', 'pawn_pair_inputs', 'half_move_clock', 'merged_king_planes')):
        feature_expression = 'feature_inputs::Inputs::new(%s, %s, %s, %s, %s, %s)' % (
            feature_expression, *(str(spec[key]).lower() for key in ('threat_inputs', 'pawn_pair_inputs', 'half_move_clock', 'merged_king_planes', 'psqt_inputs')),
        )
        auxiliary_module = '\nmod feature_inputs {\n' + indent(auxiliary, '    ') + '}\n'
    loader = (Path(__file__).parent / 'data' / 'builder_loader.rs').read_text(encoding='utf-8')
    auxiliary_module += '\nmod position_loader {\n' + indent(loader, '    ') + '}\n'
    loader_import = 'use position_loader::{ViriBinpackLoader, PositionFilter};\nuse bullet_lib::value::loader::viribinpack::Filter;'
    filtering = spec['position_filtering']
    filter_expression = '\n'.join([
        'PositionFilter {',
        '    base: Filter {',
        '        min_ply: %d, min_pieces: %d, max_eval: %d,' % (spec['min_ply'] if filtering else 0, spec['min_pieces'] if filtering else 2, spec['max_eval'] + 1 if filtering else 32769),
        '        filter_tactical: %s, filter_check: %s, filter_castling: %s,' % tuple(str(filtering and spec[key]).lower() for key in ('filter_tactical', 'filter_check', 'filter_castling')),
        '        random_fen_skipping: true, random_fen_skip_probability: %s,' % repr(spec['random_fen_skip']),
        '        ..Filter::UNRESTRICTED',
        '    },',
        '    max_ply: %d, min_eval: %d, max_pieces: %d,' % (spec['max_ply'] if filtering else 4294967295, spec['min_eval'] if filtering else 0, spec['max_pieces'] if filtering else 32),
        '    piece_count_keep: [%s],' % ', '.join(repr(p) for p in (spec['piece_count_keep'] if spec['piece_count_sampling'] else [1.0] * 31)),
        '}',
    ])
    sizes = spec['layers']
    activation = spec['activation']
    graph = [
        'let l0 = builder.new_affine("l0/", feature_count, %d);' % sizes[0],
        'l0.init_with_effective_input_size(32);',
    ]
    if spec['pairwise_activation']:
        for perspective in ('stm', 'ntm'):
            graph.extend([
                'let %s_ft = l0.forward(%s).crelu();' % (perspective, perspective),
                'let %s_hidden = %s_ft.slice_rows(0, %d) * %s_ft.slice_rows(%d, %d);' % (perspective, perspective, sizes[0] // 2, perspective, sizes[0] // 2, sizes[0]),
            ])
        graph.append('let hidden = stm_hidden.concat(ntm_hidden);')
    else:
        graph.append('let hidden = l0.forward(stm).%s().concat(l0.forward(ntm).%s());' % (activation, activation))
    last_size = sizes[0] if spec['pairwise_activation'] else sizes[0] * 2
    for index, size in enumerate(sizes[1:], 1):
        if spec['skip_connection'] and index == 2:
            graph.append('let skip = hidden;')
        graph.extend([
            'let l%d = builder.new_affine("l%d/", %d, %d);' % (index, index, last_size, size),
            'let hidden = l%d.forward(hidden).%s();' % (index, activation),
        ])
        if spec['skip_connection'] and index == 2:
            graph.append('let hidden = hidden + skip;')
        last_size = size
    heads = [(name, width) for name, width in (('score', 1), ('wdl', 3), ('uncertainty', 1)) if spec[name + '_outputs']]
    model_inputs = []
    bucket_mapping = []
    graph_pattern = '(stm, ntm)'
    for name, width in heads:
        count = spec[name + '_buckets']
        model_inputs.append('.add_sparse("%s_buckets", (%d, 1), 1)' % (name, count))
        graph_pattern = '(%s, %s_buckets)' % (graph_pattern, name)
        bucket_mapping.append('%s_buckets[0] = i32::from(MaterialCount::<%d>.bucket(pos));' % (name, count))
        graph.extend([
            'let %s_layer = builder.new_affine("%s/", %d, %d);' % (name, name, last_size, count * width),
            'let %s_output = %s_layer.forward(hidden).select(%s_buckets);' % (name, name, name),
        ])
    graph_pattern = '(%s, target)' % graph_pattern
    loss = []
    errors = []
    if spec['score_outputs']:
        loss.append('let score_mse = score_output.sigmoid().squared_error(target.slice_rows(0, 1));')
        errors.append('score_mse')
    if spec['wdl_outputs']:
        loss.extend([
            'let win = wdl_output.slice_rows(0, 1);',
            'let draw = wdl_output.slice_rows(1, 2);',
            'let loss_logit = wdl_output.slice_rows(2, 3);',
            'let maximum = win.max(draw).max(loss_logit);',
            'let win = (win - maximum).exp();',
            'let draw = (draw - maximum).exp();',
            'let loss_prob = (loss_logit - maximum).exp();',
            'let total = win + draw + loss_prob;',
            'let inverse_total = 1.0 / total;',
            'let probabilities = (win * inverse_total).concat(draw * inverse_total).concat(loss_prob * inverse_total);',
            'let wdl_mse = probabilities.squared_error(target.slice_rows(1, 4)).reduce_sum_rows() / 3.0;',
        ])
        errors.append('wdl_mse')
    if spec['uncertainty_outputs']:
        loss.append('let uncertainty_loss = uncertainty_output.squared_error(%s);' % ('score_mse' if spec['score_outputs'] else 'wdl_mse'))
        errors.append('uncertainty_loss')
    loss.append('let loss = %s;' % ' + '.join(errors))
    targets = ['target[0] = blend * f32::from(pos.result) / 2.0 + (1.0 - blend) * score;']
    if spec['wdl_outputs']:
        targets.extend([
            'let decisive = 2.0 * score - 1.0;',
            'let teacher = [decisive.max(0.0), 1.0 - decisive.abs(), (-decisive).max(0.0)];',
            'for index in 0..3 {',
            '    let result = if usize::from(pos.result) == 2 - index { 1.0 } else { 0.0 };',
            '    target[index + 1] = blend * result + (1.0 - blend) * teacher[index];',
            '}',
        ])
    formats = []
    weight_layers = ['l%d' % index for index in range(len(sizes))] + [name for name, _ in heads]
    for name in weight_layers:
        for kind in ('w', 'b'):
            expression = 'SavedFormat::id("%s/%s")' % (name, kind)
            if name != 'l0' and kind == 'w':
                expression += '.transpose()'
            if name == 'l0' and spec['feature_format'] == 'i16':
                expression += '.round().quantise::<i16>(255)'
            formats.append(expression + ',')
    def stage_rows(channel):
        return ',\n'.join('    (%d, %d, %s, %s, %d)' % (stage['start'], stage['end'], repr(stage['initial']), repr(stage['final']), ('constant', 'linear', 'cosine').index(stage['kind'])) for stage in spec[channel + '_stages'])
    source = template.substitute(
        spec, bucket_type=bucket_type, map_size=king_width * 8, bucket_rows='\n'.join(rows),
        feature_expression=feature_expression, layers=indent('\n'.join(graph), '        '),
        saved_format=indent('\n'.join(formats), '        '), lr_rows=stage_rows('lr'), wdl_rows=stage_rows('wdl'),
        dataset_ranges=', '.join('(%d, %d)' % (stage['start'], stage['end']) for stage in dataset_stages(spec)),
        worker_adapter=indent(adapter.rstrip(), '    '), auxiliary_module=auxiliary_module,
        loader_import=loader_import, target_count=4 if spec['wdl_outputs'] else 1,
        filter_expression=filter_expression,
        model_inputs=indent('\n'.join(model_inputs), '        '), graph_pattern=graph_pattern,
        bucket_mapping=indent('\n'.join(bucket_mapping), '            '),
        graph_outputs=', '.join('("%s".to_owned(), %s_output)' % (name, name) for name, _ in heads),
        loss=indent('\n'.join(loss), '        '), targets=indent('\n'.join(targets), '            '),
    )
    files = {SOURCE: source, 'LICENSE.txt': licence}
    settings = {
        **DEFAULT_SETTINGS, 'bullet_ref': BULLET_COMMIT, 'backend': spec['backend'],
        'threads': spec['threads'], 'shuffle_seed': spec['seed'], 'resume_supported': True,
        **{key: spec[key] for key in ('shuffle', 'interleave', 'shuffle_memory_mb', 'interleave_fan_in', 'dataset_shard_mb')},
        'build': [*DEFAULT_SETTINGS['build'][:-1], spec['backend']],
    }
    files[MANIFEST] = json.dumps({
        'version': 3, 'spec': spec, 'fingerprint': fingerprint(files, settings),
        'bullet_source': 'https://github.com/jw1912/bullet/tree/' + BULLET_COMMIT,
        'export': {'feature_weights': spec['feature_format'], 'feature_scale': 255 if spec['feature_format'] == 'i16' else 1, 'dense_weights': 'f32', 'dense_weights_transposed': True,
                   'heads': {name: {'buckets': spec[name + '_buckets'], 'outputs_per_bucket': width,
                                    'activation': {'score': 'sigmoid', 'wdl': 'softmax', 'uncertainty': 'identity'}[name],
                                    'weights': name + '/w', 'biases': name + '/b'} for name, width in heads},
                   'hidden_layers_bucketed': False, 'head_order': [name for name, _ in heads],
                   'threat_features': 60144 if spec['threat_inputs'] else 0,
                   'feature_activation': 'pairwise_crelu' if spec['pairwise_activation'] else spec['activation'],
                   'feature_outputs_per_perspective': sizes[0] // 2 if spec['pairwise_activation'] else sizes[0],
                   'loss': ' + '.join(errors), 'uncertainty_target': ('score_mse' if spec['score_outputs'] else 'wdl_mse') if spec['uncertainty_outputs'] else None,
                   'skip_connection': {'from_layer': 2, 'to_layer': 3, 'addition': 'after_activation', 'extra_weights': False,
                                       'source': 'https://github.com/Ciekce/Stormphrax/blob/d1468d99e3d3733100d20d682ac627998cce94b2/src/eval/nnue/arch/multilayer.h'} if spec['skip_connection'] else None,
                   'piece_square_features_per_bucket': (704 if spec['merged_king_planes'] else 768) if spec['psqt_inputs'] else 0,
                   'piece_planes': (['P', 'N', 'B', 'R', 'Q', 'K/k', 'p', 'n', 'b', 'r', 'q'] if spec['merged_king_planes'] else ['P', 'N', 'B', 'R', 'Q', 'K', 'p', 'n', 'b', 'r', 'q', 'k']) if spec['psqt_inputs'] else [],
                   'clock_features_per_bucket': 11 if spec['half_move_clock'] else 0,
                   'clock_bucket_start': 14, 'clock_bucket_step': 8, 'clock_bucket_max': 10,
                   'clock_layout': 'After piece-square features in each king bucket; inactive below 14 half-moves',
                   'clock_source': 'https://github.com/aronpetko/integral/blob/v8/src/engine/evaluation/nnue/perspective_accumulator.h'},
    }, indent=2) + '\n'
    return spec, files, validate_schedule(files, settings)


def dataset_stages(spec):
    boundaries = sorted({stage['start'] for channel in ('lr', 'wdl') for stage in spec[channel + '_stages']} | {spec['superbatches'] + 1})
    return [{'start': start, 'end': end - 1} for start, end in zip(boundaries, boundaries[1:])]


def schedule_dataset_stages(schedule):
    spec, current = builder_state(schedule)
    if not spec or not current:
        return []
    metadata = json.loads(schedule.files[MANIFEST])
    if metadata['version'] not in (2, 3):
        return []
    stored = metadata['spec']
    return dataset_stages(stored if 'lr_stages' in stored and 'wdl_stages' in stored else spec)
