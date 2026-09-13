"""Reproduce Bullet's Ranger resume bug, then verify the embedded compatibility fix.

Usage: HIP_PATH=/opt/rocm bin/python Scripts/verify_ranger_resume.py /tmp/bullet rocm
The supplied pinned Bullet checkout must be a disposable test workspace.
"""
from pathlib import Path
import subprocess
import sys
import tempfile

root = Path(sys.argv[1]).resolve()
backend = sys.argv[2] if len(sys.argv) > 2 else 'rocm'
if backend not in ('rocm', 'cuda'):
    raise SystemExit('Choose rocm or cuda')
repo = Path(__file__).resolve().parents[1]
test = (repo / 'Scripts/verify_ranger_resume.rs').read_text()
support = (repo / 'OpenBench/data/builder_ranger.rs').read_text()
original_import = 'use bullet_trainer::{optimiser::{OptimiserState, ranger::{Ranger, RangerParams}}, run::DefaultDevice};'
assert original_import in test
patched = test.replace(original_import, 'use bullet_trainer::{optimiser::OptimiserState, run::DefaultDevice};\nuse ranger_support::{Ranger, RangerParams};')
patched += '\nmod ranger_support {\n' + support + '\n}\n'
manifest = root / 'crates/bullet_lib/Cargo.toml'
text = manifest.read_text()
for name, source in [('ranger_upstream', test), ('ranger_fixed', patched)]:
    (root / 'examples' / (name + '.rs')).write_text(source)
    if 'name = "%s"' % name not in text:
        text += '\n[[example]]\nname = "%s"\npath = "../../examples/%s.rs"\n' % (name, name)
manifest.write_text(text)
for name in ('ranger_upstream', 'ranger_fixed'):
    subprocess.run(['cargo', 'build', '--release', '--locked', '-p', 'bullet_lib', '--example', name, '--features', backend], cwd=root, check=True)
    with tempfile.TemporaryDirectory(prefix='mattbench-' + name + '-') as directory:
        args = [str(root / 'target/release/examples' / name), directory]
        if name == 'ranger_upstream':
            args.append('--expect-bug')
        subprocess.run(args, check=True)
