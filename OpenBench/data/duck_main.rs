use bullet_lib::{
    game::formats::{bulletformat::ChessBoard, wakformat::bullet::DUCK_EXTRA_INDEX},
    game::inputs::{ChessBuckets, ChessBucketsMirrored, SparseInputType},
    nn::optimiser::{AdamW, AdamWParams},
    trainer::{
        save::SavedFormat,
        schedule::{TrainingSchedule, TrainingSteps, lr, wdl},
        settings::LocalSettings,
    },
    value::{ValueTrainerBuilder, loader::WakFormatLoader},
};

const L1_SIZE: usize = $l1;
const L2_SIZE: usize = $l2;
const L3_SIZE: usize = $l3;
const SCALE: f32 = $scale;
const DUCK: bool = $duck;
const MIRRORED: bool = $mirrored;
const Q0: i16 = 255;
const Q1: i16 = 128;
const Q: i16 = 64;
const FT_SHIFT: usize = 8;
const FT_SHIFT_SCALE: f32 = Q0 as f32 / ((1 << FT_SHIFT) as f32);
const I8_RANGE: f32 = i8::MAX as f32 / Q1 as f32;
const L1_RANGE: f32 = I8_RANGE * FT_SHIFT_SCALE * FT_SHIFT_SCALE;

#[derive(Clone, Copy, Default)]
struct DuckInputs;

impl SparseInputType for DuckInputs {
    type RequiredDataType = ChessBoard;

    fn num_inputs(&self) -> usize {
        768 + if DUCK { 64 } else { 0 }
    }

    fn max_active(&self) -> usize {
        32 + usize::from(DUCK)
    }

    fn map_features<F: FnMut(usize, usize)>(&self, pos: &ChessBoard, mut f: F) {
        if MIRRORED { ChessBucketsMirrored::default().map_features(pos, &mut f); } else { ChessBuckets::new([0; 64]).map_features(pos, &mut f); }
        let square = usize::from(pos.extra()[DUCK_EXTRA_INDEX]);
        if DUCK && square < 64 {
            let stm_flip = if MIRRORED && pos.our_ksq() % 8 > 3 { 7 } else { 0 };
            let ntm_flip = if MIRRORED && pos.opp_ksq() % 8 > 3 { 7 } else { 0 };
            f(768 + (square ^ stm_flip), 768 + (square ^ 56 ^ ntm_flip));
        }
    }

    fn shorthand(&self) -> String {
        format!("{}{}", self.num_inputs(), if MIRRORED { "hm" } else { "" })
    }

    fn description(&self) -> String {
        "Horizontally mirrored chess and duck inputs".to_string()
    }
}

fn main() {
    let name = std::env::var("MATTBENCH_NET_NAME").expect("Missing network name");
    let output = std::env::var("MATTBENCH_OUTPUT_DIR").expect("Missing output directory");
    std::fs::create_dir_all(&output).unwrap();
    let start: usize = std::env::var("MATTBENCH_START_SUPERBATCH").unwrap_or_else(|_| "1".into()).parse().unwrap();
    let end: usize = std::env::var("MATTBENCH_END_SUPERBATCH").unwrap_or_else(|_| "$superbatches".into()).parse().unwrap();
    assert!(start >= 1 && start <= end && end <= $superbatches, "Invalid workload bounds");
    let threads = std::env::var("MATTBENCH_THREADS").map(|value| value.parse::<usize>().expect("Invalid CPU allocation")).unwrap_or($threads).clamp(1, 255);
    let inputs = DuckInputs;
    let mut trainer = ValueTrainerBuilder::default()
        .dual_perspective()
        .optimiser(AdamW)
        .inputs(inputs)
        .save_format(&[
            SavedFormat::id("l0w").round().quantise::<i16>(Q0),
            SavedFormat::id("l0b").round().quantise::<i16>(Q0),
            SavedFormat::id("l1w")
                .transform(|_, weights| {
                    let mut permuted = vec![0.0; weights.len()];
                    for i1 in (0..L1_SIZE).step_by(4) {
                        for o in 0..L2_SIZE {
                            for i2 in 0..4 {
                                permuted[i1 * L2_SIZE + o * 4 + i2] = weights[(i1 + i2) * L2_SIZE + o];
                            }
                        }
                    }
                    for weight in &mut permuted {
                        *weight /= FT_SHIFT_SCALE * FT_SHIFT_SCALE;
                    }
                    permuted
                })
                .round()
                .quantise::<i8>(Q1),
            SavedFormat::id("l1b").round().quantise::<i32>(i32::from(Q) * 256),
            SavedFormat::id("l2w").round().quantise::<i32>(i32::from(Q)),
            SavedFormat::id("l2b").round().quantise::<i32>(i32::from(Q).pow(3)),
            SavedFormat::id("l3w").round().quantise::<i32>(i32::from(Q)),
            SavedFormat::id("l3b").round().quantise::<i32>(i32::from(Q).pow(4)),
        ])
        .loss_fn(|output, target| output.sigmoid().squared_error(target))
        .build(|builder, stm_inputs, ntm_inputs| {
            let l0 = builder.new_affine("l0", inputs.num_inputs(), L1_SIZE);
            let l1 = builder.new_affine("l1", L1_SIZE, L2_SIZE);
            let l2 = builder.new_affine("l2", L2_SIZE * 2, L3_SIZE);
            let l3 = builder.new_affine("l3", L3_SIZE, 1);

            let ft = |input, start, end| l0.slice(start, end).forward(input).crelu();
            let stm_hidden = ft(stm_inputs, 0, L1_SIZE / 2) * ft(stm_inputs, L1_SIZE / 2, L1_SIZE);
            let ntm_hidden = ft(ntm_inputs, 0, L1_SIZE / 2) * ft(ntm_inputs, L1_SIZE / 2, L1_SIZE);

            let l1_out = l1.forward(stm_hidden.concat(ntm_hidden));
            let l1_out = l1_out.concat(l1_out.abs_pow(2.0)).crelu();

            let l2_out = l2.forward(l1_out);
            let l2_out = l2_out.crelu();

            let l3_out = l3.forward(l2_out);

            l3_out
        });

    let l1_clip = AdamWParams { max_weight: L1_RANGE, min_weight: -L1_RANGE, ..Default::default() };
    trainer.optimiser.set_params_for_weight("l1w", l1_clip);

    if let Ok(path) = std::env::var("MATTBENCH_RESUME_DIR") {
        trainer.optimiser.load_from_checkpoint(&format!("{path}/optimiser_state")).unwrap();
        println!("MATTBENCH_RESUMED {start}");
    }
    let stages: &[(usize, usize)] = &[$dataset_ranges];
    for (index, &(first, last)) in stages.iter().enumerate() {
        let first = first.max(start);
        let last = last.min(end);
        if first > last { continue; }
        let list = std::env::var(format!("MATTBENCH_STAGE_{index}_FILES")).or_else(|_| std::env::var("MATTBENCH_DATA_FILES")).expect("Missing dataset list");
        let text = std::fs::read_to_string(list).expect("Cannot read dataset list");
        let paths: Vec<&str> = text.lines().filter(|line| !line.is_empty()).collect();
        assert!(!paths.is_empty(), "No dataset files");
        let loader = WakFormatLoader::new_concat_multiple(&paths, $buffer, threads, |_, mv, score, _| (!$quiet || !mv.flag().is_noisy()) && i32::from(score).abs() <= $max_eval);
        let schedule = TrainingSchedule {
            net_id: name.clone(), eval_scale: SCALE,
            steps: TrainingSteps { batch_size: $batch_size, batches_per_superbatch: $batches, start_superbatch: first, end_superbatch: last },
            wdl_scheduler: ProfileWdl,
            lr_scheduler: $lr_scheduler,
            save_rate: $save_every,
        };
        let settings = LocalSettings { threads, test_set: None, output_directory: &output, batch_queue_size: 64 };
        trainer.run(&schedule, &settings, &loader);
    }
}

#[derive(Clone, Debug)]
struct ProfileWdl;
impl wdl::WdlScheduler for ProfileWdl {
    fn blend(&self, _: usize, sb: usize, _: usize) -> f32 { stage_value(&[$wdl_rows], sb) }
    fn colourful(&self) -> String { "MattBench WDL stages".into() }
}

fn stage_value(stages: &[(usize, usize, f32, f32, u8)], sb: usize) -> f32 {
    let &(start, end, initial, final_value, kind) = stages.iter().find(|s| sb <= s.1).unwrap_or_else(|| stages.last().unwrap());
    let fraction = sb.saturating_sub(start) as f32 / (end - start).max(1) as f32;
    match kind { 0 => initial, 1 => initial + (final_value - initial) * fraction, _ => final_value + (initial - final_value) * 0.5 * (1.0 + (std::f32::consts::PI * fraction).cos()) }
}

#[derive(Clone, Debug)]
struct LegacyLR { initial: f32, final_value: f32, duration: usize, kind: u8 }
impl lr::LrScheduler for LegacyLR {
    fn lr(&self, _: usize, sb: usize) -> f32 { stage_value(&[(1, self.duration, self.initial, self.final_value, self.kind)], sb) }
    fn colourful(&self) -> String { "Legacy LR".into() }
}
