"""Validated binary export contracts for generated Bullet trainers."""

from django.core.exceptions import ValidationError


DENSE_DEFAULT = {'weight_format': 'f32', 'weight_scale': 1, 'bias_format': 'f32', 'bias_scale': 1, 'transpose': True}
INTEGER_MAX = {'i8': 127, 'i16': 32767, 'i32': 2147483647}


def dense_names(spec):
    return ['l%d' % i for i in range(1, len(spec['layers']))] + [
        name for name in ('score', 'wdl', 'uncertainty') if spec[name + '_outputs']]


def validate_export(spec):
    if spec['export_mode'] not in ('legacy', 'custom', 'heimdall'):
        raise ValidationError('Choose a valid network export mode.')
    rows = spec['dense_export']
    allowed = {'l%d' % i for i in range(1, 8)} | {'score', 'wdl', 'uncertainty'}
    if not isinstance(rows, dict) or set(rows) - allowed:
        raise ValidationError('Dense export settings must name dense layers or output heads.')
    for name, row in rows.items():
        if not isinstance(row, dict) or set(row) != set(DENSE_DEFAULT) or type(row['transpose']) is not bool:
            raise ValidationError('Complete the export settings for %s.' % name)
        for kind in ('weight', 'bias'):
            dtype, scale = row[kind + '_format'], row[kind + '_scale']
            if dtype not in ('f32', 'i8', 'i16', 'i32'):
                raise ValidationError('Choose f32, i8, i16 or i32 for %s %s.' % (name, kind))
            # Bullet's i8/i16 quantizers accept an i16 multiplier; i32 accepts i32.
            maximum = 1 if dtype == 'f32' else 2147483647 if dtype == 'i32' else 32767
            if type(scale) is not int or not 1 <= scale <= maximum:
                raise ValidationError('%s %s scale must be an integer from 1 to %d.' % (name, kind, maximum))
    if spec['export_mode'] != 'heimdall':
        return
    required = dict(psqt_inputs=True, threat_inputs=True, pawn_pair_inputs=False, half_move_clock=False,
                    merged_king_planes=False, mirrored=True, score_outputs=True, score_buckets=8,
                    wdl_outputs=False, uncertainty_outputs=False, hidden_layers_bucketed=True,
                    activation='crelu', pairwise_activation=True, pairwise_layers=[1],
                    pairwise_left_activation='crelu', pairwise_right_activation='crelu',
                    dual_activation=True, dual_layers=[2], skip_connection=False, feature_format='i16')
    if len(spec['layers']) != 3 or spec['layers'][1:] != [16, 32] or spec['layers'][0] % 128:
        raise ValidationError('Heimdall export requires L1 divisible by 128, L2=16 and L3=32.')
    mismatches = [key for key, value in required.items() if spec[key] != value]
    if mismatches:
        raise ValidationError('Heimdall export requires PSQ+TI, mirrored kings, eight bucketed score outputs, '
                              'CReLU pairwise L1 and dual L2 only. Check: %s.' % ', '.join(mismatches))


def clipping(tensor, limit):
    return ('optimiser.set_params_for_weight("%s", AdamWParams { min_weight: -(%s), '
            'max_weight: %s, ..Default::default() });' % (tensor, limit, limit))


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
            params.append(clipping(tensor, clip))
        tensors.append({'tensor': tensor, 'dtype': dtype, 'scale': scale, 'transposed': transpose,
                        'transform': transform, 'training_clip': clip})

    if spec['export_mode'] == 'heimdall':
        # The graph splits PSQ and TI so each has its own optimizer bounds.
        # FT sum and bias placement are mathematically identical to the generic graph.
        add('l0/psqt', 'i16', 255, clip='0.99')
        add('l0/ti/w', 'i8', 255, clip='127.0 / 255.0')
        add('l0/ti/b', 'i16', 255)
        add('l1/w', 'i8', 128, transform='f / ((255.0f32 / 256.0) * (255.0 / 256.0))',
            clip='(127.0 / 128.0) * (255.0 / 256.0) * (255.0 / 256.0)')
        add('l1/b', 'i32', 64 * 256)
        add('l2/w', 'i32', 64)
        add('l2/b', 'i32', 64 ** 3)
        add('score/w', 'i32', 64)
        add('score/b', 'i32', 64 ** 4)
    else:
        for kind in ('w', 'b'):
            add('l0/' + kind, spec['feature_format'], 255 if spec['feature_format'] == 'i16' else 1)
        for name in dense_names(spec):
            row = spec['dense_export'].get(name, DENSE_DEFAULT) if spec['export_mode'] == 'custom' else DENSE_DEFAULT
            for kind, suffix in (('weight', 'w'), ('bias', 'b')):
                dtype, scale = row[kind + '_format'], row[kind + '_scale']
                # Preserve AdamW's default range when it is already more restrictive.
                # 0.999999 leaves room for f32 rounding at the type boundary.
                clip = ('1.98f32.min(0.999999 * (%s.0 / %s.0))' % (INTEGER_MAX[dtype], scale)) if dtype != 'f32' else None
                add(name + '/' + suffix, dtype, scale, row['transpose'] if kind == 'weight' else False, clip=clip)
    return formats, params, tensors


def heimdall_inputs(spec):
    """Split sparse input streams without changing the feature index mapping."""
    psq_count = spec['input_buckets'] * 768
    inputs = [
        '.add_sparse("stm", (%d, 1), 32)' % psq_count,
        '.add_sparse("ntm", (%d, 1), 32)' % psq_count,
        '.add_sparse("stm_ti", (60144, 1), max_active)',
        '.add_sparse("ntm_ti", (60144, 1), max_active)',
    ]
    graph = [
        'let l0_psqt = builder.new_weights("l0/psqt", (%d, %d), bullet_trainer::model::InitSettings::Normal { mean: 0.0, stdev: (2f32 / 32.0).sqrt() });' % (spec['layers'][0], psq_count),
        'let l0_ti = builder.new_affine("l0/ti/", 60144, %d);' % spec['layers'][0],
        'l0_ti.init_with_effective_input_size(32);',
    ]
    mapping = '''let mut psq_count = 0;
let mut ti_count = 0;
features.map_features(pos, |us, them| {
    assert!(us < feature_count && them < feature_count);
    if us < PSQ_FEATURES {
        assert!(them < PSQ_FEATURES && psq_count < 32);
        stm[psq_count] = us.try_into().unwrap();
        ntm[psq_count] = them.try_into().unwrap();
        psq_count += 1;
    } else {
        assert!(them >= PSQ_FEATURES && ti_count < max_active);
        stm_ti[ti_count] = (us - PSQ_FEATURES).try_into().unwrap();
        ntm_ti[ti_count] = (them - PSQ_FEATURES).try_into().unwrap();
        ti_count += 1;
    }
});
if psq_count < 32 { stm[psq_count] = -1; ntm[psq_count] = -1; }
if ti_count < max_active { stm_ti[ti_count] = -1; ntm_ti[ti_count] = -1; }'''
    mapping = 'const PSQ_FEATURES: usize = %d;\n' % psq_count + mapping
    return inputs, graph, mapping
