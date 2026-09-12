"""Compile representative builder outputs against a local pinned Bullet checkout.

Usage: OPENBENCH_DISABLE_WATCHERS=1 bin/python Scripts/verify_builder_rust.py /tmp/bullet
The checkout is used as a disposable build workspace; no deployed service is used.
"""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'OpenSite.settings')
os.environ.setdefault('OPENBENCH_DISABLE_WATCHERS', '1')
import django
django.setup()
from OpenBench.schedule_builder import BULLET_COMMIT, DEFAULT_SPEC, generate_schedule

root = Path(sys.argv[1]).resolve()
manifest = root / 'crates/bullet_lib/Cargo.toml'
base = copy.deepcopy(DEFAULT_SPEC)
base.update(layers=[32, 16, 16], pairwise_activation=True, pairwise_layers=[1, 2, 3], skip_connection=True, wdl_outputs=True, uncertainty_outputs=True, threat_inputs=True, pawn_pair_inputs=True, half_move_clock=True, merged_king_planes=True, piece_count_sampling=True, piece_count_mode='target', piece_count_keep=[0, 0] + [1 / 31] * 31, result_filtering=True, wdl_filtered=True)
kinds = ['constant', 'linear', 'cosine', 'exponential', 'step', 'drop']
base['lr_stages'] = [{'start': 1 + i * 100, 'end': (i + 1) * 100 if i < 5 else 800, 'kind': kind, 'initial': 0.01, 'final': 0.001, 'gamma': 0.5, 'interval': 20, 'warmup_batches': 4} for i, kind in enumerate(kinds)]
variants = [('matrix', base), ('legacy', {**copy.deepcopy(base), 'lr_convention': 'legacy', 'lr_stages': copy.deepcopy(DEFAULT_SPEC['lr_stages'])}), ('unmirrored', {**copy.deepcopy(DEFAULT_SPEC), 'mirrored': False, 'input_buckets': 2, 'king_layout': [0] * 32 + [1] * 32, 'psqt_inputs': False, 'half_move_clock': True})]
sequence = copy.deepcopy(base)
sequence['lr_stages'] = [{'start': 1, 'end': 800, 'kind': 'sequence', 'segments': sequence['lr_stages']}]
variants.append(('nested_sequence', sequence))
text = manifest.read_text()
for name, spec in variants:
    name = 'verify_' + name
    _, files, _ = generate_schedule(spec)
    (root / 'examples' / (name + '.rs')).write_text(files['examples/mattbench.rs'])
    if 'name = "%s"' % name not in text:
        text += '\n[[example]]\nname = "%s"\npath = "../../examples/%s.rs"\n' % (name, name)
manifest.write_text(text)
for name, _ in variants:
    subprocess.run(['cargo', 'check', '--locked', '-p', 'bullet_lib', '--example', 'verify_' + name, '--features', 'cuda'], cwd=root, check=True)
print('All representative generated trainers compiled against Bullet ' + BULLET_COMMIT)
