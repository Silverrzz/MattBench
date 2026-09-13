//! Deterministic optimizer-only checkpoint regression (no dataset/loader effects).
//! Build against the pinned Bullet checkout with CUDA or ROCm. The optional
//! --expect-bug flag proves divergence in the unpatched upstream implementation.
use std::{collections::BTreeMap, path::PathBuf};
use bullet_compiler::tensor::TValue;
use bullet_gpu::buffer::Buffer;
use bullet_trainer::{optimiser::{OptimiserState, ranger::{Ranger, RangerParams}}, run::DefaultDevice};

fn main() {
    let args: Vec<_> = std::env::args().collect();
    let root = PathBuf::from(&args[1]);
    let expect_bug = args.iter().any(|arg| arg == "--expect-bug");
    let device = DefaultDevice::new(0).unwrap();
    let stream = device.new_stream().unwrap();
    let grads = Buffer::from_host(&device, &TValue::F32(vec![0.7, -0.3, 1.1, -0.9])).unwrap();
    let factor = Buffer::from_host(&device, &TValue::F32(vec![1.0])).unwrap();
    let lr = Buffer::from_host(&device, &TValue::F32(vec![0.03])).unwrap();
    for (k, alpha, split) in [(6, 0.5, 6), (6, 0.5, 8), (4, 0.3, 5)] {
        let params = RangerParams { k, alpha, min_weight: -0.1, max_weight: 0.1, ..Default::default() };
        let mut uninterrupted = Ranger::new(&device, 4, params).unwrap();
        let weights = Buffer::from_host(&device, &TValue::F32(vec![0.08, -0.06, 0.01, -0.03])).unwrap();
        for _ in 0..split {
            uninterrupted.update(&stream, weights.clone(), grads.clone(), factor.clone(), lr.clone()).unwrap().sync().unwrap();
        }
        let checkpoint = root.join(format!("k{k}-split{split}"));
        std::fs::create_dir(&checkpoint).unwrap();
        let checkpoint = checkpoint.to_str().unwrap();
        Ranger::write_to_checkpoint(&BTreeMap::from([("w".to_owned(), &uninterrupted)]), checkpoint).unwrap();
        let restored_weights = Buffer::from_host(&device, &weights.to_host().unwrap()).unwrap();
        let mut resumed = Ranger::new(&device, 4, params).unwrap();
        Ranger::load_from_checkpoint(&mut BTreeMap::from([("w".to_owned(), &mut resumed)]), checkpoint).unwrap();
        let mut diverged = false;
        for step in split..split + 2 * k {
            uninterrupted.update(&stream, weights.clone(), grads.clone(), factor.clone(), lr.clone()).unwrap().sync().unwrap();
            resumed.update(&stream, restored_weights.clone(), grads.clone(), factor.clone(), lr.clone()).unwrap().sync().unwrap();
            let a = weights.to_host().unwrap();
            let b = restored_weights.to_host().unwrap();
            let a = a.f32();
            let b = b.f32();
            assert!(a.iter().chain(b).all(|v| v.abs() <= 0.1), "clipping failed");
            let same = a.iter().zip(b).all(|(a, b)| a.to_bits() == b.to_bits());
            diverged |= !same;
            if !expect_bug { assert!(same, "Ranger diverged at step {}, k={k}, split={split}", step + 1); }
        }
        assert_eq!(diverged, expect_bug && split % k != 0);
        println!("k={k}, alpha={alpha}, split={split}: {}", if diverged { "upstream bug reproduced" } else { "bit-identical resume; clipping preserved" });
    }
}
