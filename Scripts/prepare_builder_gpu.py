"""Build a small GPU trainer and dataset for the optional continuation integration test.

Usage: HIP_PATH=/opt/rocm bin/python Scripts/prepare_builder_gpu.py /tmp/bullet
Then set the MATTBENCH_TEST_GPU_* variables described in docs/builder-workloads.md.
"""
import copy, os, django, sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]));os.environ['DJANGO_SETTINGS_MODULE']='OpenSite.settings';os.environ['OPENBENCH_DISABLE_WATCHERS']='1';django.setup()
from OpenBench.schedule_builder import DEFAULT_SPEC,generate_schedule
root=Path(sys.argv[1]).resolve()
spec=copy.deepcopy(DEFAULT_SPEC)
spec.update(layers=[16,8,8],pairwise_activation=True,pairwise_layers=[1,2,3],skip_connection=True,backend='rocm',threads=2,buffer_mb=16,batch_size=32,batches_per_superbatch=4,superbatches=5,save_every=5,position_filtering=False,piece_count_sampling=True,piece_count_mode='target',piece_count_keep=[0.0]*32+[1.0],wdl_filtered=True)
spec['lr_stages']=[dict(start=1,end=5,kind='sequence',segments=[dict(start=1,end=2,kind='linear',initial=0.01,final=0.001,warmup_batches=2),dict(start=3,end=5,kind='cosine',initial=0.001,final=0.0001,warmup_batches=2)])]
spec['wdl_stages']=[dict(start=1,end=3,kind='linear',initial=0.2,final=0.8),dict(start=4,end=5,kind='constant',initial=0.8,final=0.8)]
_,files,_=generate_schedule(spec)
(root/'verify_gpu_spec.json').write_text(__import__('json').dumps(spec))
(root/'examples/verify_gpu.rs').write_text(files['examples/mattbench.rs'])
(root/'examples/verify_fixture.rs').write_text('''use bullet_lib::game::formats::viriformat::{chess::board::Board, dataformat::Game};
fn main() {
 let mut board = Board::new(); board.set_startpos();
 let mv = board.parse_uci("e2e4").unwrap();
 let mut file = std::fs::File::create(std::env::args().nth(1).unwrap()).unwrap();
 for i in 0..1000 { let mut game = Game::new(&board); game.add_move(mv, (i % 100) - 50); game.serialise_into(&mut file).unwrap(); }
}
''')
p=root/'crates/bullet_lib/Cargo.toml';s=p.read_text()
for name in ['verify_gpu','verify_fixture']:
 if 'name = "'+name+'"' not in s:s+='\n[[example]]\nname = "'+name+'"\npath = "../../examples/'+name+'.rs"\n'
p.write_text(s)

import subprocess
subprocess.run(['cargo', 'build', '--locked', '--release', '-p', 'bullet_lib', '--example', 'verify_gpu', '--example', 'verify_fixture', '--features', 'rocm'], cwd=root, check=True)
subprocess.run([str(root/'target/release/examples/verify_fixture'), str(root/'verify_gpu_data.vf')], check=True)
