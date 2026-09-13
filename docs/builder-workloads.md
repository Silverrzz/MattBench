# Builder v6 and sequential training workloads

Builder manifests now use version 6. Existing schedules retain their LR interpolation and legacy export conventions when opened, and existing run snapshots are not regenerated. Existing runs get workload size 0 (uninterrupted) and priority 0 through migrations 0024–0025. New builder runs default to 50 SB per workload; custom source requires uninterrupted training. A new run may reuse an architecture-compatible checkpoint.

## Network export and clipping

The old **Feature layer export: i16** setting only quantized the combined feature weights and biases at scale 255. Dense hidden layers and all prediction heads remained float32, transposed to output-major storage. It did not produce Heimdall-compatible files or impose Heimdall's weight limits. Older schedules retain that behavior under **Generic (legacy)**.

**Custom** selects f32/i8/i16/i32, integer scales, export divisors and symmetric training clips separately for each feature tensor and dense layer/head's weights and biases. Dense weights also have a transpose toggle; biases and feature weights are never transposed. The divisor is applied to float weights before rounding and scaling, without changing the training graph. The clip limits unscaled trained values (default 1.98); integer tensors additionally receive automatic bounds accounting for their scale and divisor, with a small safety margin. Float32 scales must be 1; Bullet's i8/i16 quantization multipliers are limited to 32767 and i32 multipliers to 2147483647. Divisors and clips must be finite and between 0.000001 and 1000000. Exports round ties away from zero and reject overflow; they do not silently clamp at save time. These are weight/bias export settings, not quantization of predicted scores.

**Separate feature groups** creates independently clipped/serialized parameter tensors for PSQ/clock, pawn pairs, and threats, followed by one shared feature bias. They are summed before activation. This works with all supported feature combinations, layer sizes, activations and output heads. It does not impose any engine-specific topology. The old Heimdall preset has been removed; saved manifests using it reopen as explicit custom settings for backward compatibility.

For example, to match Heimdall's mixed integer loader, enable separate feature groups, disable pawn pairs/clock, and enter these ordinary custom settings. Leave every transpose unchecked and every divisor at 1 except where shown:

| Tensor | Format | Scale | Divisor | Training clip |
| --- | --- | --- | --- | --- |
| PSQ weights | i16 | 255 | 1 | 0.99 |
| TI weights | i8 | 255 | 1 | 127/255 |
| Feature bias | i16 | 255 | 1 | 1.98 |
| L2 weights | i8 | 128 | 0.9922027587890625 | 0.9844511747360229 |
| L2 bias | i32 | 16384 | 1 | 1.98 |
| L3 weights / bias | i32 | 64 / 262144 | 1 | 1.98 |
| Score weights / bias | i32 | 64 / 16777216 | 1 | 1.98 |

The L2 divisor is exactly `(255/256)^2`; its clip is `(127/128)*(255/256)^2`. Numeric inputs accept decimal values, not expressions. This matches export and clipping, not the reference trainer's factorizer or activity regularization. Architecture, bucket layout and evaluation scale must also match the engine.

Changing export mode or custom clipping settings is rejected on checkpoint resume. In particular, old combined-FT checkpoints cannot be loaded into the split PSQ/TI graph. Existing completed networks remain unchanged; converting/fine-tuning them is a separate operation. Manifests list every serialized tensor's type, scale, layout, transform and clipping expression, with little-endian encoding and Bullet padding to 64 bytes.

Run `Deploy/update-builder-schedule.py --help` for a guarded API updater using a local session file. It previews before saving, backs up the current builder payload with owner-only permissions, uses optimistic schedule version checks, verifies the saved settings by reloading the builder, and never starts training. `--check` only previews. Deploy the new builder before applying settings requiring new controls. The retired preset's bounds differ slightly from the generic overflow-safe clipping, so moving one of its old checkpoints to custom export requires a new run.

To check real exported bytes against raw float weights, run `bin/python Scripts/verify_builder_export.py CHECKPOINT_DIRECTORY SPEC_JSON` (requires NumPy). GPU fixture preparation accepts `--split-export` or `--custom-export`; the GPU continuation test independently verifies every exported tensor for these modes, in addition to checkpoint resume and workload boundaries.

## Optimizers and Ranger checkpoints

The Training tab offers **AdamW** (the default for new and existing schedules) and **Ranger**. Both use Bullet's decay 0.01 and beta2 0.999; AdamW uses beta1 0.9 and Ranger uses beta1 0.99. Ranger combines RAdam with Lookahead, defaulting to alpha 0.5 and interval 6 optimizer updates (batches), not superbatches. Alpha is configurable in `0.000001..1` and the interval in `1..1000000`. Export-specific clipping uses the selected optimizer's parameter type and preserves its Lookahead settings. Neither optimizer is selected automatically for an existing schedule.

Pinned Bullet `629ee50000b2afb7b3337595401c830d3b1e0f42` saves Ranger's slow weights and the inner RAdam step counter but omits the outer Lookahead counter. Resuming at step 8 with interval 6 therefore postpones the next Lookahead update from global step 12 to 14. The generated trainer embeds `builder_ranger.rs`, a small adaptation of Bullet's Ranger implementation using Bullet's RAdam and kernels, with the outer counter saved/restored as `optimiser_state/lookahead_step.txt`. It does not modify the Bullet checkout or require companion source files when downloading generated Rust. Bullet's other behavior, including zero-initialized slow weights, is retained.

Ranger archives must include `slow.bin`, `step.txt`, and `lookahead_step.txt` in addition to the normal weights/momentum/velocity and exported network. The server checks these even for uninterrupted Ranger runs. Resuming with another optimizer or changed Lookahead settings is rejected; LR/WDL/duration changes remain supported. Existing AdamW checkpoints keep their current contract.

Reproduce the upstream issue and check the compatibility fix with:

```sh
HIP_PATH=/opt/rocm bin/python Scripts/verify_ranger_resume.py /tmp/bullet rocm
```

This optimizer-only GPU test uses deterministic gradients, checks bit-identical updates and clipping across checkpoints at steps 6/8 with interval 6 and step 5 with interval 4, and excludes dataset-reader effects. The unpatched upstream case must diverge at unaligned boundaries; the embedded implementation must match at every step. Add `--ranger --split-export` to `Scripts/prepare_builder_gpu.py`, then run the GPU continuation test to check real training, export, uploads and resumed counters across workloads.

## Builder behavior

New schedules use the score output's material-count bucket selector throughout the dense hidden layers and final score head. With score output disabled, hidden layers use the WDL output's selector instead. Other heads retain their independently configured output buckets. Hidden layer sizes specify neurons per bucket; input king buckets remain separate. The **Use output buckets in hidden layers** checkbox can retain shared hidden layers. Existing schedules without this setting reopen with shared hidden layers, preserving their architecture. Changing dense bucketing requires a compatible new checkpoint or training from scratch.

**Dual activation** can be enabled on L2, L3, or both. It concatenates `CReLU(x)` and `CReLU(x²)` after bucket selection, matching Heimdall's `x.concat(x.abs_pow(2.0)).crelu()`. In particular, negative inputs still contribute to the squared branch. Each selected layer doubles its output width. Pairwise activation remains a separate operation that multiplies activated halves; the two cannot be selected on the same layer. Skip connections compare widths after activation. Checkpoint resumption requires the same dual activation layers.

For Heimdall's activation and dense-bucketing structure, use layers `512, 16, 32` (or override L1 in the engine), eight score output buckets, hidden output bucketing, pairwise CReLU × CReLU on the feature layer, dual activation on L2, and CReLU elsewhere. Set the custom export controls separately as above.

Both LR and WDL editors accept lengths or inclusive boundaries. The run total is explicit: changing it does not adjust stages. Each channel must cover that total independently. Adding a stage splits the last stage; removing one transfers its duration to its neighbor. Entry modes are presentation metadata; execution uses canonical inclusive ranges. Dataset selectors use the union of both channels’ boundaries.

Each native LR stage can use Constant, Linear, Cosine, Exponential, Step, Drop, or **Sequence**. Choose Sequence and add segments to combine curves within one stage: an 800-SB stage can contain cosine for 50 SB followed by linear for 750 SB. Each segment has its own curve parameters and optional Warmup. Internal segment boundaries do not create dataset stages or restart the reader. Segment lengths must sum to their containing stage’s length; changing an outer length leaves segments unchanged and reports the exact mismatch. Entry mode also applies to segments; their canonical inclusive ranges start at 1 within the containing stage. Up to 64 segments are supported. Explicitly adding/removing outer stages splits or extends the contained segments along with the outer range.

The generator composes pinned Bullet’s `lr::Sequence` with indexing local to each curve. Warmup fits within a curve’s first SB and follows Bullet’s native batch formula. Legacy constant/linear/cosine schedules use an inclusive interpolation adapter; Sequence requires the native convention. Changing the convention explicitly changes LR behavior. Training and telemetry share the same scheduler instance, and workload resumption preserves its global position.

Piece-count arrays contain 33 values indexed 0–32. Legacy 31-value keep arrays gain two leading zeros. Fixed mode applies keep probabilities; target mode counts eligible positions per loader worker across conversion chunks, then accepts with `clamp(0.5 * target / observed_frequency, 0, 1)`. Target values are preserved and must sum to 1 within 0.00001. Reader restarts reset the counters.

Imports accept numeric arrays, Rust declarations with bracketed arrays, comments and trailing commas. King layouts accept 32 values (files a–d mirrored per rank) or 64 values. Choose a1=0 or a8=0 explicitly. Canonical storage always uses a1=0 and the board displays rank 8 at the top. Mirrored 64-square imports must be symmetric. IDs must be contiguous from zero. Click/wheel changes and drag painting are undoable; dragging captures the source square’s value.

Position/move filters, result consistency, WDL-model sampling, and piece-count sampling are independent groups. WDL sampling uses viriformat 2.0.1’s default coefficients and is separate from WDL loss blending. Model validation checks coefficients, normalization, material bounds, scale and denominator positivity at extrema across the material interval.

## Worker protocol and persistence

Protocol 4 workers advertise `training-workloads` and `typed-assignments`. Protocol 3 workers continue to receive uninterrupted runs. Training requests carry `X-Training-Claim` for sliced runs. Reports, artifacts, checkpoint registration, dataset preparation and completion are fenced against expired claims. Artifact names include the claim UUID; logs and manifests are stored per attempt. The run retains cumulative history and overall progress.

Each workload takes the latest committed checkpoint, starts at the next SB, and intersects its execution with dataset stages. Generated trainers honor `MATTBENCH_START_SUPERBATCH` and `MATTBENCH_END_SUPERBATCH`, restore weights/momentum/velocity, and force a complete checkpoint at the workload end. LR and WDL remain indexed globally. Dataset reading, shuffling and adaptive counters restart.

The worker exits the trainer and finishes uploads before completion. The server verifies the boundary checkpoint, log and manifest, then atomically releases the worker and either requeues the same run or completes it. Repeated completion is idempotent. Heartbeat expiry fences an attempt and resumes only committed work. A crash after the final checkpoint can finish the remaining artifacts without training the final SB again. Continuation checkpoints are protected from pruning.

Training and tests share `only`, `force`, priority and `focus` refinement. Highest priority wins after `force`. Equal-priority categories alternate; training runs rotate by least-recent allocation, while tests retain throughput balancing. Priority edits apply at the next allocation. Test assignments are also replayable by claim ID.

Dataset caches use pinned source identities, preparation settings and tool revisions. Trainer caches also include source, Bullet commit, backend, environment and runtime. Cached data and executables are checksum verified before reuse. Active entries move into the attempt directory; inactive entries can be evicted to satisfy disk requirements.

## Verification

Run the Python suite and pure browser-operation tests:

```sh
bin/python manage.py test OpenBench.tests
node Scripts/test_builder_values.cjs
```

Use an isolated PostgreSQL database to exercise concurrent claims and duplicate completion; the concurrency tests skip on SQLite. The GPU integration test is opt-in.

Browser tests need Playwright and Chromium on the module search path:

```sh
node Scripts/test_builder_browser.cjs
```

`PLAYWRIGHT_CHROMIUM_EXECUTABLE` can point to an existing Chromium executable. The script serves the actual builder template with an in-memory schedule on an ephemeral localhost port.

For Rust verification, create a disposable checkout at Bullet `629ee50000b2afb7b3337595401c830d3b1e0f42` and supply its path:

```sh
CUDA_PATH=/opt/cuda bin/python Scripts/verify_builder_rust.py /tmp/bullet
HIP_PATH=/opt/rocm bin/python Scripts/verify_builder_lr.py /tmp/bullet rocm
HIP_PATH=/opt/rocm bin/python Scripts/prepare_builder_gpu.py /tmp/bullet
MATTBENCH_TEST_GPU_BINARY=/tmp/bullet/target/release/examples/verify_gpu \
MATTBENCH_TEST_GPU_SPEC=/tmp/bullet/verify_gpu_spec.json \
MATTBENCH_TEST_GPU_DATA=/tmp/bullet/verify_gpu_data.vf \
bin/python manage.py test OpenBench.tests.test_gpu_continuation
```

Repeat GPU fixture preparation with `--dual` and rerun the continuation test to exercise dual activation on both L2 and L3, output-bucketed hidden layers, the feature-layer pairwise activation, and a skip connection together.

The GPU fixture uses ROCm, five SB, small batches, an LR sequence within one stage, pairwise layers, WDL filtering and adaptive sampling. It alternates two runs, restores optimizer checkpoints, and verifies LR/WDL numbering and cumulative history on the first run through completion. Numerical identity across reader restarts is not required.

Implementation verification passed on the local AMD RX 6600 XT: run 1 SB 1–2, run 2 SB 1–2, run 1 SB 3–4, run 2 SB 3–4, run 1 SB 5. Browser gestures/imports/save-reload, all six schedulers, optimizer checkpoint restoration, PostgreSQL concurrent claims/completion, stale uploads, retention and legacy-worker handling were exercised.

For an authorized dev deployment, update `/opt/mattbench-dev` while preserving its modified configuration, migrate, collect static files to `/var/www/MattBenchDev/static`, and restart only `mattbench-dev-web` and `mattbench-dev-training`. Browser and GPU checks there precede any separately authorized production deployment.
