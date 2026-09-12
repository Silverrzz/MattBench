"""Compare generated LR scheduling with pinned Bullet at every batch and resume SB.

Usage: HIP_PATH=/opt/rocm bin/python Scripts/verify_builder_lr.py /tmp/bullet rocm
"""
from pathlib import Path
import os,sys,copy
sys.path.insert(0,str(Path(__file__).resolve().parents[1]));os.environ['OPENBENCH_DISABLE_WATCHERS']='1';os.environ['DJANGO_SETTINGS_MODULE']='OpenSite.settings'
import django;django.setup()
from OpenBench.schedule_builder import DEFAULT_SPEC,generate_schedule,validate_spec,lr_scheduler
root=Path(sys.argv[1]).resolve()
spec=copy.deepcopy(DEFAULT_SPEC);start=1;stages=[]
for kind,duration in zip(['constant','linear','cosine','exponential','step','drop'],[1,2,3,4,5,6]):
 stages.append(dict(start=start,end=start+duration-1,kind=kind,initial=0.01,final=0.001,gamma=0.5,interval=2,warmup_batches=2));start+=duration
spec.update(lr_stages=stages,superbatches=21,wdl_stages=[dict(start=1,end=21,kind='linear',initial=0.2,final=0.8)])
spec,files,_=generate_schedule(spec)
s=files['examples/mattbench.rs'];s+='''
#[test]
fn generated_sequence_matches_pinned_schedulers_and_resumed_steps() {
    let generated = EXPR;
    let native: Vec<Box<dyn Fn(bullet_trainer::run::Step) -> f32>> = vec![
        lr::Warmup { inner: lr::ConstantLR { value: 0.01 }, warmup_batches: 2 }.boxed(),
        lr::Warmup { inner: lr::LinearDecayLR { initial_lr: 0.01, final_lr: 0.001, final_superbatch: 2 }, warmup_batches: 2 }.boxed(),
        lr::Warmup { inner: lr::CosineDecayLR { initial_lr: 0.01, final_lr: 0.001, final_superbatch: 3 }, warmup_batches: 2 }.boxed(),
        lr::Warmup { inner: lr::ExponentialDecayLR { initial_lr: 0.01, final_lr: 0.001, final_superbatch: 4 }, warmup_batches: 2 }.boxed(),
        lr::Warmup { inner: lr::StepLR { start: 0.01, gamma: 0.5, step: 2 }, warmup_batches: 2 }.boxed(),
        lr::Warmup { inner: lr::DropLR { start: 0.01, gamma: 0.5, drop: 2 }, warmup_batches: 2 }.boxed(),
    ];
    let ranges = [(1,1), (2,3), (4,6), (7,10), (11,15), (16,21)];
    for resume_start in 1..=21 {
        for sb in resume_start..=21 {
            let index = ranges.iter().position(|&(start,end)| start <= sb && sb <= end).unwrap();
            for batch in 0..4 {
                let local = bullet_trainer::run::Step::new(sb - ranges[index].0 + 1, batch, ranges[index].1 - ranges[index].0 + 1, 4);
                assert_eq!(generated.lr(batch, sb), native[index](local));
                let global = bullet_trainer::run::Step::new(sb, batch, 21, 4);
                assert_eq!(generated.clone().boxed()(global), generated.lr(batch, sb));
            }
        }
    }
    assert_eq!(generated.lr(0, 1), 0.005);
    assert_eq!(generated.lr(0, 4), native[2](bullet_trainer::run::Step::new(1,0,3,4)));
}
#[test]
fn legacy_single_and_endpoints_are_preserved() {
    for kind in 0..3 {
        let legacy = LegacyLR { initial: 0.01, final_value: 0.001, duration: 100, kind };
        assert_eq!(legacy.lr(0, 1), 0.01);
        assert!((legacy.lr(0, 100) - if kind == 0 { 0.01 } else { 0.001 }).abs() < 1e-8);
        let one = LegacyLR { duration: 1, ..legacy };
        assert_eq!(one.lr(0, 1), 0.01);
    }
}
'''.replace('EXPR',lr_scheduler(spec))
# Place all six curves inside a single dataset stage, following an initial stage.
# Include the requested 50-SB cosine segment and a one-SB segment.
nested = copy.deepcopy(spec)
segments = copy.deepcopy(stages)
start = 1
for segment, duration in zip(segments, [1, 2, 50, 4, 5, 6]):
 segment.update(start=start, end=start + duration - 1)
 start += duration
nested.update(superbatches=73, lr_stages=[
 dict(start=1, end=3, kind='constant', initial=0.02, final=0.02),
 dict(start=4, end=71, kind='sequence', segments=segments),
 dict(start=72, end=73, kind='linear', initial=0.001, final=0.0001),
], wdl_stages=[dict(start=1, end=73, kind='constant', initial=0.5, final=0.5)])
nested = validate_spec(nested)
s += '''
#[test]
fn nested_sequence_keeps_local_indices_across_outer_stages_and_resumes() {
    let generated = EXPR;
    let native: Vec<Box<dyn Fn(bullet_trainer::run::Step) -> f32>> = vec![
        lr::ConstantLR { value: 0.02 }.boxed(),
        lr::Warmup { inner: lr::ConstantLR { value: 0.01 }, warmup_batches: 2 }.boxed(),
        lr::Warmup { inner: lr::LinearDecayLR { initial_lr: 0.01, final_lr: 0.001, final_superbatch: 2 }, warmup_batches: 2 }.boxed(),
        lr::Warmup { inner: lr::CosineDecayLR { initial_lr: 0.01, final_lr: 0.001, final_superbatch: 50 }, warmup_batches: 2 }.boxed(),
        lr::Warmup { inner: lr::ExponentialDecayLR { initial_lr: 0.01, final_lr: 0.001, final_superbatch: 4 }, warmup_batches: 2 }.boxed(),
        lr::Warmup { inner: lr::StepLR { start: 0.01, gamma: 0.5, step: 2 }, warmup_batches: 2 }.boxed(),
        lr::Warmup { inner: lr::DropLR { start: 0.01, gamma: 0.5, drop: 2 }, warmup_batches: 2 }.boxed(),
        lr::LinearDecayLR { initial_lr: 0.001, final_lr: 0.0001, final_superbatch: 2 }.boxed(),
    ];
    let ranges = [(1,3), (4,4), (5,6), (7,56), (57,60), (61,65), (66,71), (72,73)];
    for resume_start in 1..=73 {
        for sb in resume_start..=73 {
            let index = ranges.iter().position(|&(start,end)| start <= sb && sb <= end).unwrap();
            for batch in 0..4 {
                let local = bullet_trainer::run::Step::new(sb - ranges[index].0 + 1, batch, ranges[index].1 - ranges[index].0 + 1, 4);
                assert_eq!(generated.lr(batch, sb), native[index](local), "SB {sb}, batch {batch}");
                let global = bullet_trainer::run::Step::new(sb, batch, 73, 4);
                assert_eq!(generated.clone().boxed()(global), generated.lr(batch, sb));
            }
        }
    }
}
'''.replace('EXPR', lr_scheduler(nested))
(root/'examples/verify_lr.rs').write_text(s)
p=root/'crates/bullet_lib/Cargo.toml';s=p.read_text()
if 'name = "verify_lr"' not in s:p.write_text(s+'\n[[example]]\nname = "verify_lr"\npath = "../../examples/verify_lr.rs"\n')

import subprocess
subprocess.run(['cargo', 'test', '--locked', '--release', '-p', 'bullet_lib', '--example', 'verify_lr', '--features', sys.argv[2] if len(sys.argv) > 2 else 'cuda'], cwd=root, check=True)
