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
    'pairwise_layers': [1], 'pairwise_left_activation': 'crelu', 'pairwise_right_activation': 'crelu',
    'random_fen_skip': 0.0, 'position_filtering': True,
    'min_ply': 16, 'max_ply': 100000, 'min_eval': 0, 'max_eval': 31338,
    'min_pieces': 4, 'max_pieces': 32, 'filter_tactical': True, 'filter_check': True, 'filter_castling': False,
    'piece_count_sampling': False, 'piece_count_mode': 'fixed', 'piece_count_keep': [1.0] * 33,
    'result_filtering': False, 'max_eval_incorrectness': 2500,
    'wdl_filtered': False,
    'wdl_model_params_a': [6.87155862, -39.65226391, 90.68460352, 170.66996364],
    'wdl_model_params_b': [-7.19890710, 56.13947185, -139.91091183, 182.81007427],
    'material_min': 17, 'material_max': 78, 'mom_target': 58, 'wdl_heuristic_scale': 1.5,
    'lr_convention': 'native', 'presentation': {'lr': 'lengths', 'wdl': 'lengths'},
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
        value = {'lr_convention': 'legacy', 'presentation': {'lr': 'boundaries', 'wdl': 'boundaries'},
                 **{key: DEFAULT_SPEC[key] for key in ('piece_count_mode', 'result_filtering', 'max_eval_incorrectness',
                    'wdl_filtered', 'wdl_model_params_a', 'wdl_model_params_b', 'material_min', 'material_max', 'mom_target', 'wdl_heuristic_scale')}, **value}
        value = {'psqt_inputs': True, **value}
        value = {'pairwise_layers': [1],
                 'pairwise_left_activation': 'crelu', 'pairwise_right_activation': 'crelu', **value}
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
        ('max_eval_incorrectness', 0, 4294967295), ('material_min', 0, 4294967295),
        ('material_max', 0, 4294967295), ('mom_target', 1, 4294967295),
    ):
        if type(spec[key]) is not int or not minimum <= spec[key] <= maximum:
            raise ValidationError('%s must be between %d and %d.' % (key.replace('_', ' ').capitalize(), minimum, maximum))
    for key in ('mirrored', 'psqt_inputs', 'threat_inputs', 'pawn_pair_inputs', 'shuffle', 'interleave',
                'score_outputs', 'wdl_outputs', 'uncertainty_outputs', 'half_move_clock', 'merged_king_planes', 'skip_connection', 'pairwise_activation',
                'position_filtering', 'filter_tactical', 'filter_check', 'filter_castling', 'piece_count_sampling', 'result_filtering', 'wdl_filtered'):
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
    if isinstance(keep, list) and len(keep) == 31:
        keep = [0.0, 0.0] + keep
    if spec['piece_count_mode'] not in ('fixed', 'target'):
        raise ValidationError('Choose fixed keep probabilities or a target distribution.')
    if not isinstance(keep, list) or len(keep) != 33 or any(type(p) not in (int, float) or not math.isfinite(p) or not 0 <= p <= 1 for p in keep):
        raise ValidationError('Set a finite value between 0 and 1 for each piece count from 0 to 32.')
    if spec['piece_count_mode'] == 'target' and not math.isclose(sum(keep), 1, rel_tol=0, abs_tol=0.00001):
        raise ValidationError('Target proportions must sum to approximately one (within 0.00001).')
    spec['piece_count_keep'] = [float(p) for p in keep]
    low, high = (spec['min_pieces'], spec['max_pieces']) if spec['position_filtering'] else (2, 32)
    if spec['piece_count_sampling'] and not any(keep[low:high + 1]):
        raise ValidationError('Keep at least one piece count in the permitted range.')
    from OpenBench.builder_values import validate_wdl_model
    validate_wdl_model(spec)
    if spec['lr_convention'] not in ('native', 'legacy'):
        raise ValidationError('Invalid LR convention.')
    if not isinstance(spec['presentation'], dict) or set(spec['presentation']) != {'lr', 'wdl'} or any(mode not in ('lengths', 'boundaries') for mode in spec['presentation'].values()):
        raise ValidationError('Choose lengths or boundaries for each stage editor.')
    for key, options in (
        ('activation', ('screlu', 'crelu')),
        ('pairwise_left_activation', ('crelu', 'screlu', 'relu', 'identity')),
        ('pairwise_right_activation', ('crelu', 'screlu', 'relu', 'identity')),
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
    pairwise_layers = spec['pairwise_layers']
    if not isinstance(pairwise_layers, list) or not pairwise_layers or any(type(layer) is not int or layer not in (1, 2, 3) for layer in pairwise_layers) or len(set(pairwise_layers)) != len(pairwise_layers):
        raise ValidationError('Choose valid pairwise layers.')
    spec['pairwise_layers'] = sorted(pairwise_layers)
    if spec['pairwise_activation'] and any(layer > len(layers) or layers[layer - 1] % 2 for layer in pairwise_layers):
        raise ValidationError('Each selected pairwise layer must exist and have an even number of neurons.')
    effective_sizes = [size // 2 if spec['pairwise_activation'] and index + 1 in pairwise_layers else size for index, size in enumerate(layers)]
    if spec['skip_connection'] and (len(layers) < 3 or effective_sizes[1] != effective_sizes[2]):
        raise ValidationError('The skip connection requires Layer 2 and Layer 3 to have equal output widths after activation.')
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
        if isinstance(stages[-1], dict) and type(stages[-1].get('end')) is int and stages[-1]['end'] > spec['superbatches']:
            raise ValidationError('%s stages allocate %d SB; %d SB excess over the run total.' % (channel.upper(), stages[-1]['end'], stages[-1]['end'] - spec['superbatches']))
        next_start = 1
        for index, stage in enumerate(stages, 1):
            label = '%s stage %d' % (channel.upper(), index)
            required = {'start', 'end', 'kind', 'initial', 'final'}
            optional = {'gamma', 'interval', 'warmup_batches'} if channel == 'lr' else set()
            if not isinstance(stage, dict) or not required <= set(stage) or set(stage) - required - optional:
                raise ValidationError('%s is incomplete.' % label)
            if type(stage['start']) is not int or type(stage['end']) is not int or stage['start'] != next_start or not stage['start'] <= stage['end'] <= spec['superbatches']:
                raise ValidationError('%s must start at SB %d and end within the schedule, without gaps or overlaps.' % (label, next_start))
            if stage['kind'] not in (('constant', 'linear', 'cosine', 'exponential', 'step', 'drop') if channel == 'lr' else ('constant', 'linear', 'cosine')):
                raise ValidationError('%s has an invalid curve.' % label)
            if channel == 'lr':
                if spec['lr_convention'] == 'legacy' and stage['kind'] not in ('constant', 'linear', 'cosine'):
                    raise ValidationError('Legacy interpolation supports constant, linear and cosine curves.')
                for key, default in (('gamma', 0.5), ('interval', 1), ('warmup_batches', 0)):
                    stage.setdefault(key, default)
                if type(stage['gamma']) not in (float, int) or not math.isfinite(stage['gamma']) or not 0 <= stage['gamma'] <= 1:
                    raise ValidationError('%s gamma must be between zero and one.' % label)
                if type(stage['interval']) is not int or not 1 <= stage['interval'] <= 1000000:
                    raise ValidationError('%s interval must be a positive integer.' % label)
                if type(stage['warmup_batches']) is not int or not 0 <= stage['warmup_batches'] <= spec['batches_per_superbatch']:
                    raise ValidationError('%s warmup must fit within its first superbatch.' % label)
            for key in ('initial', 'final'):
                if type(stage[key]) not in (int, float) or not math.isfinite(stage[key]) or not 0 <= stage[key] <= 1:
                    raise ValidationError('%s values must be between 0 and 1.' % label)
                stage[key] = float(stage[key])
            if channel == 'lr' and stage['kind'] == 'exponential' and min(stage['initial'], stage['final']) <= 0:
                raise ValidationError('%s exponential rates must be positive.' % label)
            next_start = stage['end'] + 1
        if next_start != spec['superbatches'] + 1:
            raise ValidationError('%s stages allocate %d SB; %d SB short of the run total.' % (channel.upper(), next_start - 1, spec['superbatches'] - next_start + 1))
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
        if metadata['version'] not in (1, 2, 3, 4):
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
        '        max_eval_incorrectness: %d, wdl_filtered: %s,' % (spec['max_eval_incorrectness'] if spec['result_filtering'] else 4294967295, str(spec['wdl_filtered']).lower()),
        *('        %s: [%s],' % (key, ', '.join(repr(float(x)) for x in spec[key])) for key in ('wdl_model_params_a', 'wdl_model_params_b')),
        '        material_min: %d, material_max: %d, mom_target: %d, wdl_heuristic_scale: %s,' % (spec['material_min'], spec['material_max'], spec['mom_target'], repr(float(spec['wdl_heuristic_scale']))),
        '        ..Filter::UNRESTRICTED',
        '    },',
        '    max_ply: %d, min_eval: %d, max_pieces: %d,' % (spec['max_ply'] if filtering else 4294967295, spec['min_eval'] if filtering else 0, spec['max_pieces'] if filtering else 32),
        '    piece_count_keep: [%s],' % ', '.join(repr(p) for p in (spec['piece_count_keep'] if spec['piece_count_sampling'] else [1.0] * 33)),
        '    target_distribution: %s,' % str(spec['piece_count_sampling'] and spec['piece_count_mode'] == 'target').lower(),
        '}',
    ])
    sizes = spec['layers']
    activation = spec['activation']
    pairwise_layers = spec['pairwise_layers'] if spec['pairwise_activation'] else []
    def pairwise_expression(node, size):
        halves = []
        for side, start, end in (('left', 0, size // 2), ('right', size // 2, size)):
            expression = '%s.slice_rows(%d, %d)' % (node, start, end)
            selected_activation = spec['pairwise_' + side + '_activation']
            if selected_activation != 'identity':
                expression += '.%s()' % selected_activation
            halves.append(expression)
        return ' * '.join(halves)
    graph = [
        'let l0 = builder.new_affine("l0/", feature_count, %d);' % sizes[0],
        'l0.init_with_effective_input_size(32);',
    ]
    if 1 in pairwise_layers:
        for perspective in ('stm', 'ntm'):
            graph.extend([
                'let %s_ft = l0.forward(%s);' % (perspective, perspective),
                'let %s_hidden = %s;' % (perspective, pairwise_expression(perspective + '_ft', sizes[0])),
            ])
        graph.append('let hidden = stm_hidden.concat(ntm_hidden);')
    else:
        graph.append('let hidden = l0.forward(stm).%s().concat(l0.forward(ntm).%s());' % (activation, activation))
    last_size = sizes[0] if 1 in pairwise_layers else sizes[0] * 2
    for index, size in enumerate(sizes[1:], 1):
        if spec['skip_connection'] and index == 2:
            graph.append('let skip = hidden;')
        graph.extend([
            'let l%d = builder.new_affine("l%d/", %d, %d);' % (index, index, last_size, size),
            'let preactivation = l%d.forward(hidden);' % index,
            'let hidden = %s;' % (pairwise_expression('preactivation', size) if index + 1 in pairwise_layers else 'preactivation.%s()' % activation),
        ])
        if spec['skip_connection'] and index == 2:
            graph.append('let hidden = hidden + skip;')
        last_size = size // 2 if index + 1 in pairwise_layers else size
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
        saved_format=indent('\n'.join(formats), '        '), lr_scheduler=lr_scheduler(spec), wdl_rows=stage_rows('wdl'),
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
        'version': 4, 'spec': spec, 'fingerprint': fingerprint(files, settings),
        'workload_bounds': True,
        'bullet_source': 'https://github.com/jw1912/bullet/tree/' + BULLET_COMMIT,
        'export': {'feature_weights': spec['feature_format'], 'feature_scale': 255 if spec['feature_format'] == 'i16' else 1, 'dense_weights': 'f32', 'dense_weights_transposed': True,
                   'heads': {name: {'buckets': spec[name + '_buckets'], 'outputs_per_bucket': width,
                                    'activation': {'score': 'sigmoid', 'wdl': 'softmax', 'uncertainty': 'identity'}[name],
                                    'weights': name + '/w', 'biases': name + '/b'} for name, width in heads},
                   'hidden_layers_bucketed': False, 'head_order': [name for name, _ in heads],
                   'threat_features': (59808 if spec['pawn_pair_inputs'] else 60144) if spec['threat_inputs'] else 0,
                   'feature_activation': 'pairwise' if 1 in pairwise_layers else spec['activation'],
                   'feature_outputs_per_perspective': sizes[0] // 2 if 1 in pairwise_layers else sizes[0],
                   'pairwise': {'layers': pairwise_layers, 'left_activation': spec['pairwise_left_activation'], 'right_activation': spec['pairwise_right_activation'], 'operation': 'multiply_activated_halves'},
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
    if metadata['version'] not in (2, 3, 4):
        return []
    stored = metadata['spec']
    return dataset_stages(stored if 'lr_stages' in stored and 'wdl_stages' in stored else spec)


def lr_scheduler(spec):
    def scheduler(stage):
        duration = stage['end'] - stage['start'] + 1
        initial, final = repr(float(stage['initial'])), repr(float(stage['final']))
        kind = stage['kind']
        if spec['lr_convention'] == 'legacy':
            expression = 'LegacyLR { initial: %s, final_value: %s, duration: %d, kind: %d }' % (initial, final, duration, ('constant', 'linear', 'cosine').index(kind))
        elif kind == 'constant':
            expression = 'lr::ConstantLR { value: %s }' % initial
        elif kind in ('step', 'drop'):
            expression = 'lr::%sLR { start: %s, gamma: %s, %s: %d }' % (kind.capitalize(), initial, repr(float(stage['gamma'])), kind, stage['interval'])
        else:
            expression = 'lr::%sDecayLR { initial_lr: %s, final_lr: %s, final_superbatch: %d }' % (kind.capitalize(), initial, final, duration)
        if stage['warmup_batches']:
            expression = 'lr::Warmup { inner: %s, warmup_batches: %d }' % (expression, stage['warmup_batches'])
        return expression
    stages = spec['lr_stages']
    expression = scheduler(stages[-1])
    for stage in reversed(stages[:-1]):
        expression = 'lr::Sequence { first: %s, second: %s, first_scheduler_final_superbatch: %d }' % (scheduler(stage), expression, stage['end'] - stage['start'] + 1)
    return expression
