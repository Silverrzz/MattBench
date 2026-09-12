# Builder v4 and sequential training workloads

Builder manifests now use version 4. Existing schedules retain their LR interpolation convention when opened, and existing run snapshots are not regenerated. Existing runs get workload size 0 (uninterrupted) and priority 0 through migrations 0024–0025. New builder runs default to 50 SB per workload; custom source requires uninterrupted training. A new run may reuse an architecture-compatible checkpoint.

## Builder behavior

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

The GPU fixture uses ROCm, five SB, small batches, an LR sequence within one stage, pairwise layers, WDL filtering and adaptive sampling. It alternates two runs, restores optimizer checkpoints, and verifies LR/WDL numbering and cumulative history on the first run through completion. Numerical identity across reader restarts is not required.

Implementation verification passed on the local AMD RX 6600 XT: run 1 SB 1–2, run 2 SB 1–2, run 1 SB 3–4, run 2 SB 3–4, run 1 SB 5. Browser gestures/imports/save-reload, all six schedulers, optimizer checkpoint restoration, PostgreSQL concurrent claims/completion, stale uploads, retention and legacy-worker handling were exercised.

For an authorized dev deployment, update `/opt/mattbench-dev` while preserving its modified configuration, migrate, collect static files to `/var/www/MattBenchDev/static`, and restart only `mattbench-dev-web` and `mattbench-dev-training`. Browser and GPU checks there precede any separately authorized production deployment.
