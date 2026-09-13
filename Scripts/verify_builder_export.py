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
    features = ((704 if spec['merged_king_planes'] else 768) if spec['psqt_inputs'] else 0) + (11 if spec['half_move_clock'] else 0)
    features *= spec['input_buckets']
    groups = [(name, size) for name, size in [('psqt', features), ('pp', 4560 if spec['pawn_pair_inputs'] else 0),
              ('ti', (59808 if spec['pawn_pair_inputs'] else 60144) if spec['threat_inputs'] else 0)] if size]
    feature_rows = {}
    if spec.get('split_features'):
        shapes = []
        for index, (name, size) in enumerate(groups):
            tensor = 'l0/' + name + ('/w' if index == len(groups) - 1 else '')
            shapes.append((tensor, (size, l1)))
            feature_rows[tensor] = spec.get('feature_export', {}).get(name)
        tensor = 'l0/' + groups[-1][0] + '/b'
        shapes.append((tensor, (l1,)))
        feature_rows[tensor] = spec.get('feature_export', {}).get('bias')
    else:
        shapes = [('l0/w', (sum(size for _, size in groups), l1)), ('l0/b', (l1,))]
        feature_rows = {tensor: spec.get('feature_export', {}).get(name) for name, tensor in [('combined', 'l0/w'), ('bias', 'l0/b')]}
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
    for index, (tensor, shape) in enumerate(shapes):
        count = int(np.prod(shape))
        values = raw[raw_offset:raw_offset + count].reshape(shape)
        raw_offset += count
        assert np.isfinite(values).all(), tensor
        if tensor.startswith('l0/'):
            row = feature_rows[tensor] if spec.get('export_mode') == 'custom' else None
            if row is None:
                dtype = spec['feature_format']
                scale = 255 if dtype == 'i16' else 1
            else:
                dtype, scale = row['format'], row['scale']
        else:
            name, suffix = tensor.split('/')
            row = spec.get('dense_export', {}).get(name, {}) if spec.get('export_mode') == 'custom' else {}
            kind = 'weight' if suffix == 'w' else 'bias'
            dtype, scale = row.get(kind + '_format', 'f32'), row.get(kind + '_scale', 1)
            if suffix == 'w' and row.get('transpose', True):
                values = values.T
            row = {key: row.get(kind + '_' + key, default) for key, default in [('divisor', 1), ('clip', 1.98)]}
        if row is not None and spec.get('export_mode') == 'custom':
            divisor = np.float32(row.get('divisor', 1))
            limit = np.float32(row.get('clip', 1.98))
            if dtype != 'f32':
                maximum = {'i8': 127, 'i16': 32767, 'i32': 2147483647}[dtype]
                limit = min(limit, np.float32(0.999999) * (np.float32(maximum) / np.float32(scale)) * divisor)
            assert np.abs(values).max() <= limit, tensor
            if divisor != 1:
                values = values / divisor
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
