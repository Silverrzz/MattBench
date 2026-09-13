"""Validated binary export contracts for generated Bullet trainers."""

import math

from django.core.exceptions import ValidationError


DENSE_DEFAULT = {'weight_format': 'f32', 'weight_scale': 1, 'bias_format': 'f32', 'bias_scale': 1, 'transpose': True}
TENSOR_DEFAULT = {'format': 'f32', 'scale': 1, 'divisor': 1.0, 'clip': 1.98}
INTEGER_MAX = {'i8': 127, 'i16': 32767, 'i32': 2147483647}


def dense_names(spec):
    return ['l%d' % i for i in range(1, len(spec['layers']))] + [
        name for name in ('score', 'wdl', 'uncertainty') if spec[name + '_outputs']]


def upgrade_export(value):
    """Read the retired preset as ordinary editable settings, never generate it."""
    value = {'export_mode': 'legacy', 'dense_export': {}, 'split_features': False, 'feature_export': {}, **value}
    if value['export_mode'] == 'heimdall':
        value.update(export_mode='custom', split_features=True, feature_export={
            'psqt': dict(format='i16', scale=255, divisor=1.0, clip=0.99),
            'ti': dict(format='i8', scale=255, divisor=1.0, clip=127 / 255),
            'bias': dict(format='i16', scale=255, divisor=1.0, clip=1.98)}, dense_export={
                'l1': dict(weight_format='i8', weight_scale=128, weight_divisor=(255 / 256) ** 2,
                           weight_clip=(127 / 128) * (255 / 256) ** 2, bias_format='i32', bias_scale=16384, transpose=False),
                'l2': dict(weight_format='i32', weight_scale=64, bias_format='i32', bias_scale=262144, transpose=False),
                'score': dict(weight_format='i32', weight_scale=64, bias_format='i32', bias_scale=16777216, transpose=False)})
    return value


def validate_tensor(row, name):
    if not isinstance(row, dict) or set(row) != set(TENSOR_DEFAULT):
        raise ValidationError('Complete the export settings for %s.' % name)
    dtype, scale = row['format'], row['scale']
    if dtype not in ('f32', 'i8', 'i16', 'i32'):
        raise ValidationError('Choose f32, i8, i16 or i32 for %s.' % name)
    maximum = 1 if dtype == 'f32' else 2147483647 if dtype == 'i32' else 32767
    if type(scale) is not int or not 1 <= scale <= maximum:
        raise ValidationError('%s scale must be an integer from 1 to %d.' % (name, maximum))
    for key in ('divisor', 'clip'):
        if type(row[key]) not in (float, int) or not math.isfinite(row[key]) or not 0.000001 <= row[key] <= 1000000:
            raise ValidationError('%s %s must be between 0.000001 and 1000000.' % (name, key))
        row[key] = float(row[key])


def validate_export(spec):
    if spec['export_mode'] not in ('legacy', 'custom'):
        raise ValidationError('Choose a valid network export mode.')
    if type(spec['split_features']) is not bool:
        raise ValidationError('Choose whether to split feature groups.')
    if spec['split_features'] and spec['export_mode'] != 'custom':
        raise ValidationError('Separate feature groups require custom export settings.')
    rows = spec['feature_export']
    if not isinstance(rows, dict) or set(rows) - {'combined', 'psqt', 'pp', 'ti', 'bias'}:
        raise ValidationError('Feature export settings must name feature groups or their shared bias.')
    for name, row in rows.items():
        validate_tensor(row, name)
    rows = spec['dense_export']
    allowed = {'l%d' % i for i in range(1, 8)} | {'score', 'wdl', 'uncertainty'}
    if not isinstance(rows, dict) or set(rows) - allowed:
        raise ValidationError('Dense export settings must name dense layers or output heads.')
    extra = {kind + '_' + key: default for kind in ('weight', 'bias') for key, default in (('divisor', 1.0), ('clip', 1.98))}
    for name, row in rows.items():
        if not isinstance(row, dict) or not set(DENSE_DEFAULT) <= set(row) or set(row) - (set(DENSE_DEFAULT) | set(extra)) or type(row['transpose']) is not bool:
            raise ValidationError('Complete the export settings for %s.' % name)
        row = rows[name] = {**extra, **row}
        for kind in ('weight', 'bias'):
            tensor = {key: row[kind + '_' + key] for key in TENSOR_DEFAULT}
            validate_tensor(tensor, name + ' ' + kind)
            row.update({kind + '_' + key: value for key, value in tensor.items()})


def clipping(tensor, limit, optimizer):
    params = 'RangerParams' if optimizer == 'ranger' else 'AdamWParams'
    return ('optimiser.set_params_for_weight("%s", %s { min_weight: -(%s), '
            'max_weight: %s, ..base_optimiser_params });' % (tensor, params, limit, limit))


def export_recipe(spec):
    """Return Rust save expressions, optimizer settings and per-tensor metadata.

    Integer exports reject overflow; never silently clamp checkpoint exports.
    Training-time clipping keeps quantized tensors within representable bounds.
    """
    formats, params, tensors = [], [], []

    def add(tensor, dtype, scale, transpose=False, transform=None, clip=None):
        expression = 'SavedFormat::id("%s")' % tensor
        if transpose:
            expression += '.transpose()'
        if transform:
            expression += '.transform(|_, values| values.iter().map(|f| %s).collect())' % transform
        if dtype != 'f32':
            expression += '.round().quantise::<%s>(%d)' % (dtype, scale)
        formats.append(expression + ',')
        if clip is not None:
            params.append(clipping(tensor, clip, spec['optimizer']))
        tensors.append({'tensor': tensor, 'dtype': dtype, 'scale': scale, 'transposed': transpose,
                        'transform': transform, 'training_clip': clip})

    def custom(tensor, row, transpose=False):
        dtype, scale, divisor = row['format'], row['scale'], row.get('divisor', 1.0)
        clip = '%sf32' % repr(float(row.get('clip', 1.98)))
        if dtype != 'f32':
            clip += '.min(0.999999 * (%s.0 / %s.0) * %sf32)' % (INTEGER_MAX[dtype], scale, repr(float(divisor)))
        transform = 'f / %sf32' % repr(float(divisor)) if divisor != 1 else None
        add(tensor, dtype, scale, transpose, transform, clip)

    for group, tensor in feature_tensors(spec):
        row = spec['feature_export'].get(group) if spec['export_mode'] == 'custom' else None
        if row is None:
            add(tensor, spec['feature_format'], 255 if spec['feature_format'] == 'i16' else 1)
        else:
            custom(tensor, row)
    for name in dense_names(spec):
        row = spec['dense_export'].get(name, DENSE_DEFAULT) if spec['export_mode'] == 'custom' else DENSE_DEFAULT
        for kind, suffix in (('weight', 'w'), ('bias', 'b')):
            transpose = row['transpose'] if kind == 'weight' else False
            if spec['export_mode'] == 'legacy':
                add(name + '/' + suffix, 'f32', 1, transpose)
            else:
                custom(name + '/' + suffix, {key: row.get(kind + '_' + key, default) for key, default in TENSOR_DEFAULT.items()}, transpose)
    return formats, params, tensors


def feature_groups(spec):
    """Contiguous groups in Inputs' index order: bucketed PSQ/clock, PP, TI."""
    base = ((704 if spec['merged_king_planes'] else 768) if spec['psqt_inputs'] else 0) + (11 if spec['half_move_clock'] else 0)
    groups = [('psqt', base * spec['input_buckets'], 32 * int(spec['psqt_inputs']) + int(spec['half_move_clock'])),
              ('pp', 4560 if spec['pawn_pair_inputs'] else 0, 120),
              ('ti', (59808 if spec['pawn_pair_inputs'] else 60144) if spec['threat_inputs'] else 0, 128)]
    return [(name, size, active) for name, size, active in groups if size]


def feature_tensors(spec):
    if not spec['split_features']:
        return [('combined', 'l0/w'), ('bias', 'l0/b')]
    groups = feature_groups(spec)
    return [(name, 'l0/' + name + ('/w' if i == len(groups) - 1 else ''))
            for i, (name, _, _) in enumerate(groups)] + [('bias', 'l0/' + groups[-1][0] + '/b')]


def split_inputs(spec):
    """Split sparse streams by feature family, with one shared bias before activation."""
    groups = feature_groups(spec)
    inputs, graph, mapping, pattern, sums = [], [], [], [], {}
    for perspective in ('stm', 'ntm'):
        terms = []
        for i, (name, size, active) in enumerate(groups):
            stream = perspective + '_' + name
            inputs.append('.add_sparse("%s", (%d, 1), %d)' % (stream, size, active))
            pattern.append(stream)
            terms.append('l0_%s.%s(%s)' % (name, 'forward' if i == len(groups) - 1 else 'matmul', stream))
        sums[perspective] = ' + '.join(terms)
    for i, (name, size, active) in enumerate(groups):
        if i == len(groups) - 1:
            graph += ['let l0_%s = builder.new_affine("l0/%s/", %d, %d);' % (name, name, size, spec['layers'][0]),
                      'l0_%s.init_with_effective_input_size(32);' % name]
        else:
            graph.append('let l0_%s = builder.new_weights("l0/%s", (%d, %d), bullet_trainer::model::InitSettings::Normal { mean: 0.0, stdev: (2f32 / 32.0).sqrt() });' % (name, name, spec['layers'][0], size))
        mapping.append('let mut %s_count = 0;' % name)
    mapping += ['features.map_features(pos, |us, them| {', '    assert!(us < feature_count && them < feature_count);']
    offset = 0
    for i, (name, size, active) in enumerate(groups):
        mapping += ['    %sif us < %d {' % ('else ' if i else '', offset + size),
                    '        assert!(them >= %d && them < %d && %s_count < %d);' % (offset, offset + size, name, active),
                    '        stm_%s[%s_count] = (us - %d).try_into().unwrap();' % (name, name, offset),
                    '        ntm_%s[%s_count] = (them - %d).try_into().unwrap();' % (name, name, offset),
                    '        %s_count += 1;' % name, '    }']
        offset += size
    mapping.append('});')
    for name, _, active in groups:
        mapping.append('if %s_count < %d { stm_%s[%s_count] = -1; ntm_%s[%s_count] = -1; }' % (name, active, name, name, name, name))
    nested = pattern[0]
    for name in pattern[1:]:
        nested = '(%s, %s)' % (nested, name)
    return inputs, graph, '\n'.join(mapping), nested, sums
