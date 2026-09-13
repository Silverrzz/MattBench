"""Explicit generic export settings used by integration tests, not UI presets."""


def integer_export():
    return dict(export_mode='custom', split_features=True, feature_export={
        'psqt': dict(format='i16', scale=255, divisor=1.0, clip=0.99),
        'ti': dict(format='i8', scale=255, divisor=1.0, clip=127 / 255),
        'bias': dict(format='i16', scale=255, divisor=1.0, clip=1.98)}, dense_export={
            'l1': dict(weight_format='i8', weight_scale=128, weight_divisor=(255 / 256) ** 2,
                       weight_clip=(127 / 128) * (255 / 256) ** 2, bias_format='i32', bias_scale=16384, transpose=False),
            'l2': dict(weight_format='i32', weight_scale=64, bias_format='i32', bias_scale=262144, transpose=False),
            'score': dict(weight_format='i32', weight_scale=64, bias_format='i32', bias_scale=16777216, transpose=False)})
