use bullet_lib::{
    game::{
        formats::bulletformat::ChessBoard,
        inputs::{SparseInputType, $bucket_type},
        outputs::{MaterialCount, OutputBuckets},
    },
    value::{loader::{ViriBinpackLoader, viribinpack::Filter}, save::save_to_checkpoint},
};
use bullet_trainer::{
    model::{ModelDefinition, ModelInputs, ModelInputsMapper, ModelWeights, SavedFormat},
    optimiser::{Optimiser, adam::{AdamW, AdamWParams}},
    reader::ReadMapLoader,
    run::{DefaultDevice, TrainingSchedule, TrainingSteps, train},
};

const KING_BUCKETS: [usize; $map_size] = [
$bucket_rows
];
const OUTPUT_BUCKETS: usize = $output_buckets;
const SUPERBATCHES: usize = $superbatches;
const BATCH_SIZE: usize = $batch_size;
const BATCHES_PER_SUPERBATCH: usize = $batches_per_superbatch;
const SAVE_EVERY: usize = $save_every;
const EVAL_SCALE: f32 = $eval_scale;
const THREADS: usize = $threads;

const LR_STAGES: &[(usize, usize, f32, f32, u8)] = &[
$lr_rows
];
const WDL_STAGES: &[(usize, usize, f32, f32, u8)] = &[
$wdl_rows
];
const DATASET_STAGES: &[(usize, usize)] = &[$dataset_ranges];

fn stage_value(stages: &[(usize, usize, f32, f32, u8)], superbatch: usize) -> f32 {
    let &(start, end, initial, final_value, kind) = stages.iter().find(|s| superbatch <= s.1).unwrap();
    let progress = superbatch.saturating_sub(start) as f32 / (end - start).max(1) as f32;
    let blend = match kind {
        0 => 0.0,
        2 => (1.0 - (std::f32::consts::PI * progress).cos()) / 2.0,
        _ => progress,
    };
    initial + blend * (final_value - initial)
}

fn main() {
    let job = mattbench::Run::load();
    let features = $feature_expression;
    let feature_count = features.num_inputs();
    let max_active = features.max_active();
    let inputs = ModelInputs::default()
        .add_sparse("stm", (feature_count, 1), max_active)
        .add_sparse("ntm", (feature_count, 1), max_active)
        .add_sparse("buckets", (OUTPUT_BUCKETS, 1), 1)
        .add_dense("targets", (1, 1));
    let definition = ModelDefinition::build(&inputs, |builder, (((stm, ntm), buckets), target)| {
$layers
        let loss = output.sigmoid().squared_error(target);
        (Some(loss.reduce_sum_batch()), vec![("output".to_owned(), output)])
    });
    let weights = ModelWeights::new(&definition, $seed);
    let device = DefaultDevice::new(0).unwrap();
    let mut optimiser = Optimiser::<_, AdamW<_>>::new(definition, weights, device, AdamWParams::default()).unwrap();
    let saved_format = vec![
$saved_format
    ];
    if let Some(path) = &job.resume {
        optimiser.load_from_checkpoint(&format!("{path}/optimiser_state")).unwrap();
        job.resumed();
    }
    for (index, &(start, end)) in DATASET_STAGES.iter().enumerate() {
        if job.start > end { continue; }
        let list = std::env::var(format!("MATTBENCH_STAGE_{}_FILES", index)).expect("Missing stage dataset");
        let stage_data = std::fs::read_to_string(list).expect("Cannot read stage dataset");
        let paths: Vec<&str> = stage_data.lines().filter(|path| !path.is_empty()).collect();
        assert!(!paths.is_empty(), "Empty stage dataset");
        let features = $feature_expression;
        let mapper = ModelInputsMapper::build(&inputs, move |pos: &ChessBoard, step, (((stm, ntm), bucket), target)| {
            let mut count = 0;
            features.map_features(pos, |us, them| {
                assert!(us < feature_count && them < feature_count);
                stm[count] = us.try_into().unwrap();
                ntm[count] = them.try_into().unwrap();
                count += 1;
            });
            if count < max_active {
                stm[count] = -1;
                ntm[count] = -1;
            }
            bucket[0] = i32::from(MaterialCount::<OUTPUT_BUCKETS>.bucket(pos));
            let blend = stage_value(WDL_STAGES, step.superbatch());
            let score = 1.0 / (1.0 + (-f32::from(pos.score) / EVAL_SCALE).exp());
            target[0] = blend * f32::from(pos.result) / 2.0 + (1.0 - blend) * score;
        });
        let reader = ViriBinpackLoader::new_concat_multiple(&paths, $buffer_mb, THREADS, Filter::default());
        let mut loss_sum = 0.0;
        train(
            &mut optimiser,
            TrainingSchedule {
                steps: TrainingSteps { batch_size: BATCH_SIZE, batches_per_superbatch: BATCHES_PER_SUPERBATCH, start_superbatch: job.start.max(start), end_superbatch: end },
                lr_schedule: Box::new(|step| stage_value(LR_STAGES, step.superbatch())),
                log_rate: 128,
            },
            ReadMapLoader::new(reader, mapper, THREADS as u8),
            |_, step, loss| {
                loss_sum += loss;
                if step.batch() + 1 == BATCHES_PER_SUPERBATCH {
                    let loss = loss_sum / BATCHES_PER_SUPERBATCH as f32;
                    let superbatch = step.superbatch();
                    let progress = 100.0 * superbatch as f32 / SUPERBATCHES as f32;
                    let lr = stage_value(LR_STAGES, superbatch);
                    println!("MATTBENCH_METRIC {{\"loss\":{loss},\"superbatch\":{superbatch},\"progress\":{progress},\"learning_rate\":{lr}}}");
                    loss_sum = 0.0;
                }
            },
            |optimiser, step| {
                let superbatch = step.superbatch();
                if superbatch.is_multiple_of(SAVE_EVERY) || superbatch == SUPERBATCHES {
                    let name = format!("{}-{superbatch}", job.name);
                    save_to_checkpoint(optimiser, &saved_format, &format!("{}/{name}", job.output));
                    job.saved(&name, superbatch);
                }
            },
        ).unwrap();
    }
    if job.start > SUPERBATCHES {
        let name = format!("{}-{SUPERBATCHES}", job.name);
        save_to_checkpoint(&optimiser, &saved_format, &format!("{}/{name}", job.output));
        job.saved(&name, SUPERBATCHES);
    }
}

mod mattbench {
$worker_adapter
}
$auxiliary_module
