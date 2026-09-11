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
    'layers': [768], 'activation': 'screlu', 'threat_inputs': False, 'pawn_pair_inputs': False,
    'input_buckets': 1, 'mirrored': True, 'king_layout': [0] * 64, 'output_buckets': 8,
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
        ('input_buckets', 1, 64), ('output_buckets', 1, 32), ('batch_size', 1, 1048576),
        ('batches_per_superbatch', 1, 1000000), ('superbatches', 1, 1000000), ('save_every', 1, 1000000),
        ('threads', 1, 255), ('buffer_mb', 16, 65536), ('seed', 0, 2 ** 53 - 1),
        ('shuffle_memory_mb', 16, 65536), ('interleave_fan_in', 2, 256),
        ('dataset_shard_mb', 4, 16384),
    ):
        if type(spec[key]) is not int or not minimum <= spec[key] <= maximum:
            raise ValidationError('%s must be between %d and %d.' % (key.replace('_', ' ').capitalize(), minimum, maximum))
    for key in ('mirrored', 'threat_inputs', 'pawn_pair_inputs', 'shuffle', 'interleave'):
        if type(spec[key]) is not bool:
            raise ValidationError('Invalid %s selection.' % key.replace('_', ' '))
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
        next_start = 1
        for index, stage in enumerate(stages, 1):
            label = '%s stage %d' % (channel.upper(), index)
            if not isinstance(stage, dict) or set(stage) != {'start', 'end', 'kind', 'initial', 'final'}:
                raise ValidationError('%s is incomplete.' % label)
            if type(stage['start']) is not int or type(stage['end']) is not int or stage['start'] != next_start or not stage['start'] <= stage['end'] <= spec['superbatches']:
                raise ValidationError('%s must start at SB %d and end within the schedule, without gaps or overlaps.' % (label, next_start))
            if stage['kind'] not in ('constant', 'linear', 'cosine'):
                raise ValidationError('%s has an invalid curve.' % label)
            for key in ('initial', 'final'):
                if type(stage[key]) not in (int, float) or not math.isfinite(stage[key]) or not 0 <= stage[key] <= 1:
                    raise ValidationError('%s values must be between 0 and 1.' % label)
                stage[key] = float(stage[key])
            if stage['kind'] == 'constant':
                stage['final'] = stage['initial']
            if stage['start'] == stage['end'] and stage['initial'] != stage['final']:
                raise ValidationError('%s needs at least two superbatches to change value.' % label)
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
        if metadata['version'] not in (1, 2):
            return None, False
        spec = validate_spec(metadata['spec'])
        return spec, metadata['fingerprint'] == fingerprint(schedule.files, schedule.settings)
    except (KeyError, TypeError, ValueError, ValidationError):
        return None, False


def generate_schedule(value):
    spec = validate_spec(value)
    template, auxiliary, adapter, licence = assets()
    bucket_type = 'ChessBucketsMirrored' if spec['mirrored'] else 'ChessBuckets'
    width = 4 if spec['mirrored'] else 8
    rows = ['    ' + ', '.join(str(bucket) for bucket in spec['king_layout'][rank * 8:rank * 8 + width]) + ',' for rank in range(8)]
    feature_expression = '%s::new(KING_BUCKETS)' % bucket_type
    auxiliary_module = ''
    if spec['threat_inputs'] or spec['pawn_pair_inputs']:
        feature_expression = 'feature_inputs::Inputs::new(%s, %s, %s)' % (
            feature_expression, str(spec['threat_inputs']).lower(), str(spec['pawn_pair_inputs']).lower(),
        )
        auxiliary_module = '\nmod feature_inputs {\n' + indent(auxiliary, '    ') + '}\n'
    sizes = spec['layers']
    activation = spec['activation']
    graph = [
        'let l0 = builder.new_affine("l0/", feature_count, %d);' % sizes[0],
        'l0.init_with_effective_input_size(32);',
        'let hidden = l0.forward(stm).%s().concat(l0.forward(ntm).%s());' % (activation, activation),
    ]
    last_size = sizes[0] * 2
    for index, size in enumerate(sizes[1:], 1):
        graph.extend([
            'let l%d = builder.new_affine("l%d/", %d, OUTPUT_BUCKETS * %d);' % (index, index, last_size, size),
            'let hidden = l%d.forward(hidden).select(buckets).%s();' % (index, activation),
        ])
        last_size = size
    graph.extend([
        'let output_layer = builder.new_affine("l%d/", %d, OUTPUT_BUCKETS);' % (len(sizes), last_size),
        'let output = output_layer.forward(hidden).select(buckets);',
    ])
    formats = []
    for index in range(len(sizes) + 1):
        for kind in ('w', 'b'):
            expression = 'SavedFormat::id("l%d/%s")' % (index, kind)
            if index and kind == 'w':
                expression += '.transpose()'
            if index == 0 and spec['feature_format'] == 'i16':
                expression += '.round().quantise::<i16>(255)'
            formats.append(expression + ',')
    def stage_rows(channel):
        return ',\n'.join('    (%d, %d, %s, %s, %d)' % (stage['start'], stage['end'], repr(stage['initial']), repr(stage['final']), ('constant', 'linear', 'cosine').index(stage['kind'])) for stage in spec[channel + '_stages'])
    source = template.substitute(
        spec, bucket_type=bucket_type, map_size=width * 8, bucket_rows='\n'.join(rows),
        feature_expression=feature_expression, layers=indent('\n'.join(graph), '        '),
        saved_format=indent('\n'.join(formats), '        '), lr_rows=stage_rows('lr'), wdl_rows=stage_rows('wdl'),
        dataset_ranges=', '.join('(%d, %d)' % (stage['start'], stage['end']) for stage in dataset_stages(spec)),
        worker_adapter=indent(adapter.rstrip(), '    '), auxiliary_module=auxiliary_module,
    )
    files = {SOURCE: source, 'LICENSE.txt': licence}
    settings = {
        **DEFAULT_SETTINGS, 'bullet_ref': BULLET_COMMIT, 'backend': spec['backend'],
        'threads': spec['threads'], 'shuffle_seed': spec['seed'], 'resume_supported': True,
        **{key: spec[key] for key in ('shuffle', 'interleave', 'shuffle_memory_mb', 'interleave_fan_in', 'dataset_shard_mb')},
        'build': [*DEFAULT_SETTINGS['build'][:-1], spec['backend']],
    }
    files[MANIFEST] = json.dumps({
        'version': 2, 'spec': spec, 'fingerprint': fingerprint(files, settings),
        'bullet_source': 'https://github.com/jw1912/bullet/tree/' + BULLET_COMMIT,
        'export': {'feature_weights': spec['feature_format'], 'feature_scale': 255 if spec['feature_format'] == 'i16' else 1, 'dense_weights': 'f32', 'dense_weights_transposed': True},
    }, indent=2) + '\n'
    return spec, files, validate_schedule(files, settings)


def dataset_stages(spec):
    boundaries = sorted({stage['start'] for channel in ('lr', 'wdl') for stage in spec[channel + '_stages']} | {spec['superbatches'] + 1})
    return [{'start': start, 'end': end - 1} for start, end in zip(boundaries, boundaries[1:])]


def schedule_dataset_stages(schedule):
    spec, current = builder_state(schedule)
    return dataset_stages(spec) if spec and current and json.loads(schedule.files[MANIFEST])['version'] == 2 else []
