"""Independently verify a checkpoint's raw float tensors against exported bytes.

Usage: bin/python Scripts/verify_builder_export.py CHECKPOINT_DIRECTORY SPEC_JSON
Requires NumPy. Does not use the builder's export recipe for expectations.
"""
import json
from pathlib import Path
import sys

import numpy as np


def verify_export(directory, spec):
    directory = Path(directory)
    raw = np.memmap(directory / 'raw.bin', dtype='<f4', mode='r')
    binary = (directory / 'quantised.bin').read_bytes()
    raw_offset = byte_offset = 0
    l1 = spec['layers'][0]
    heimdall = spec.get('export_mode') == 'heimdall'
    if heimdall:
        shapes = [('l0/psqt', (spec['input_buckets'] * 768, l1)), ('l0/ti/w', (60144, l1)), ('l0/ti/b', (l1,))]
    else:
        features = ((704 if spec['merged_king_planes'] else 768) if spec['psqt_inputs'] else 0) + (11 if spec['half_move_clock'] else 0)
        features *= spec['input_buckets']
        features += ((59808 if spec['pawn_pair_inputs'] else 60144) if spec['threat_inputs'] else 0) + (4560 if spec['pawn_pair_inputs'] else 0)
        shapes = [('l0/w', (features, l1)), ('l0/b', (l1,))]
    pairwise = spec['pairwise_layers'] if spec['pairwise_activation'] else []
    dual = spec['dual_layers'] if spec['dual_activation'] else []
    width = l1 if 1 in pairwise else 2 * l1
    buckets = spec['score_buckets'] if spec['score_outputs'] else spec['wdl_buckets']
    if not spec['hidden_layers_bucketed']:
        buckets = 1
    for i, size in enumerate(spec['layers'][1:], 1):
        shapes += [('l%d/w' % i, (width, size * buckets)), ('l%d/b' % i, (size * buckets,))]
        width = size // 2 if i + 1 in pairwise else size * 2 if i + 1 in dual else size
    for name, outputs in [('score', 1), ('wdl', 3), ('uncertainty', 1)]:
        if spec[name + '_outputs']:
            count = outputs * spec[name + '_buckets']
            shapes += [(name + '/w', (width, count)), (name + '/b', (count,))]
    integer_recipe = [('i16', 255), ('i8', 255), ('i16', 255), ('i8', 128), ('i32', 16384),
                      ('i32', 64), ('i32', 262144), ('i32', 64), ('i32', 16777216)]
    for index, (tensor, shape) in enumerate(shapes):
        count = int(np.prod(shape))
        values = raw[raw_offset:raw_offset + count].reshape(shape)
        raw_offset += count
        assert np.isfinite(values).all(), tensor
        if heimdall:
            dtype, scale = integer_recipe[index]
            if tensor == 'l0/psqt':
                assert np.abs(values).max() <= np.float32(0.99), tensor
            if tensor == 'l0/ti/w':
                assert np.abs(values).max() <= np.float32(127) / np.float32(255), tensor
            if tensor == 'l1/w':
                ratio = np.float32(255) / np.float32(256)
                assert np.abs(values).max() <= np.float32(127 / 128) * ratio * ratio, tensor
                values = values / np.float32(ratio * ratio)
        elif tensor.startswith('l0/'):
            dtype = spec['feature_format']
            scale = 255 if dtype == 'i16' else 1
        else:
            name, suffix = tensor.split('/')
            row = spec.get('dense_export', {}).get(name, {}) if spec.get('export_mode') == 'custom' else {}
            kind = 'weight' if suffix == 'w' else 'bias'
            dtype, scale = row.get(kind + '_format', 'f32'), row.get(kind + '_scale', 1)
            if suffix == 'w' and row.get('transpose', True):
                values = values.T
        if dtype == 'f32':
            expected = values.astype('<f4').tobytes()
        else:
            scaled = values.astype(np.float64) * scale
            rounded = np.copysign(np.floor(np.abs(scaled) + 0.5), scaled)
            storage = {'i8': 'i1', 'i16': '<i2', 'i32': '<i4'}[dtype]
            bounds = np.iinfo(storage)
            assert rounded.min() >= bounds.min and rounded.max() <= bounds.max, tensor
            expected = rounded.astype(storage).tobytes()
        assert binary[byte_offset:byte_offset + len(expected)] == expected, tensor
        byte_offset += len(expected)
    assert raw_offset == raw.size
    padding = (-byte_offset) % 64
    assert binary[byte_offset:] == (b'bullet' * 11)[:padding]
    assert len(binary) == byte_offset + padding
    return len(binary)


if __name__ == '__main__':
    size = verify_export(sys.argv[1], json.loads(Path(sys.argv[2]).read_text()))
    print('Verified all tensors, quantization, layout, training bounds and padding: %d bytes' % size)
