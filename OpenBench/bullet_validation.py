import copy
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from django.conf import settings
from django.db import transaction

from OpenBench.bullet import BULLET_REPOSITORY
from OpenBench.models import TrainingConfiguration, TrainingSchedule


def trainer_specs():
    from OpenBench.schedule_builder import DEFAULT_SPEC, builder_state
    changes = [
        {},
        {'layers': [32, 16, 16], 'pairwise_activation': True, 'pairwise_layers': [1, 2, 3],
         'skip_connection': True, 'wdl_outputs': True, 'uncertainty_outputs': True,
         'threat_inputs': True, 'pawn_pair_inputs': True, 'half_move_clock': True, 'merged_king_planes': True},
        {'mirrored': False, 'input_buckets': 2, 'king_layout': [0] * 32 + [1] * 32, 'psqt_inputs': False, 'half_move_clock': True},
        {'layers': [512, 16, 32], 'pairwise_activation': True, 'dual_activation': True, 'activation': 'crelu', 'threat_inputs': True},
        {'layers': [32, 8, 8], 'hidden_layers_bucketed': False, 'score_outputs': False, 'wdl_outputs': True, 'wdl_buckets': 4, 'uncertainty_outputs': True, 'uncertainty_buckets': 2},
        {'optimizer': 'ranger', 'ranger_alpha': 0.3, 'ranger_k': 4},
        {'layers': [32, 8], 'optimizer': 'ranger', 'export_mode': 'custom', 'dense_export': {
            'score': {'weight_format': 'i8', 'weight_scale': 128, 'bias_format': 'i32', 'bias_scale': 16384, 'transpose': False}}},
        {'loader_interleave': False},
    ]
    specs = [{**copy.deepcopy(DEFAULT_SPEC), **change} for change in changes]
    for schedule in TrainingSchedule.objects.filter(settings__bullet_repo=BULLET_REPOSITORY).iterator():
        spec, generated = builder_state(schedule)
        if spec is not None and generated:
            specs.append(spec)
    return list({json.dumps(spec, sort_keys=True): spec for spec in specs}.values())


def check_revision(commit, stop_event=None):
    from OpenBench.schedule_builder import SOURCE, generate_schedule
    if not re.fullmatch('[0-9a-f]{40}', commit):
        raise ValueError('Invalid Bullet commit.')
    root = Path(os.environ.get('MATTBENCH_BULLET_CHECK_ROOT', str(Path(settings.TRAINING_ROOT) / '_bullet_compatibility')))
    root.mkdir(parents=True, exist_ok=True)
    cargo = os.environ.get('MATTBENCH_CARGO') or shutil.which('cargo')
    if not cargo:
        raise RuntimeError('Rust Cargo is unavailable to the training coordinator. Install the configured Rust toolchain before retrying.')
    environment = {key: os.environ[key] for key in ('PATH', 'HOME', 'USERPROFILE', 'SYSTEMROOT', 'RUSTUP_HOME', 'SSL_CERT_FILE', 'CARGO_HTTP_CAINFO') if key in os.environ}
    environment.update(CARGO_HOME=str(root / 'cargo'), CARGO_TARGET_DIR=str(root / 'target'), CARGO_BUILD_JOBS='2', CARGO_TERM_COLOR='never', GIT_TERMINAL_PROMPT='0')
    deadline = time.monotonic() + 1500
    with tempfile.TemporaryDirectory(prefix='revision-', dir=root) as temporary:
        repository = Path(temporary)
        log_path = root / ('check-%s.log' % commit)
        with log_path.open('w+', encoding='utf-8') as log:
            def run(command):
                if stop_event is not None and stop_event.is_set():
                    raise RuntimeError('Compatibility check interrupted. Retry the revision.')
                remaining = max(1, int(deadline - time.monotonic()))
                result = subprocess.run(command, cwd=repository, env=environment, stdout=log, stderr=subprocess.STDOUT, timeout=remaining)
                if result.returncode:
                    log.flush()
                    with log_path.open('rb') as source:
                        source.seek(max(0, log_path.stat().st_size - 6000))
                        detail = source.read().decode('utf-8', errors='replace')
                    raise RuntimeError('Generated trainers are incompatible with this revision.\n' + detail)
            run(['git', 'init', '--quiet'])
            run(['git', 'fetch', '--quiet', '--depth=1', BULLET_REPOSITORY, commit])
            run(['git', 'checkout', '--quiet', '--detach', 'FETCH_HEAD'])
            manifest = repository / 'crates/bullet_lib/Cargo.toml'
            if not manifest.is_file():
                raise RuntimeError('The selected revision has no supported bullet_lib package.')
            names = []
            entries = []
            for index, spec in enumerate(trainer_specs()):
                name = 'mattbench_compatibility_%d' % index
                _, files, _ = generate_schedule(spec, commit)
                path = repository / 'examples' / (name + '.rs')
                path.parent.mkdir(exist_ok=True)
                path.write_text(files[SOURCE], encoding='utf-8')
                names.append(name)
                entries.append('\n[[example]]\nname = %s\npath = %s\n' % (json.dumps(name), json.dumps('../../examples/' + name + '.rs')))
            with manifest.open('a', encoding='utf-8') as output:
                output.write(''.join(entries))
            command = [cargo, 'check', '--package', 'bullet_lib', '--no-default-features']
            for name in names:
                command += ['--example', name]
            run(command)
    return 'Compatibility passed for %d generated trainer configurations. New schedules use this revision; existing schedules are unchanged.' % len(names)


def validate_pending_revision(stop_event):
    with transaction.atomic():
        config = TrainingConfiguration.objects.select_for_update().filter(pk=1, bullet_validation='QUEUED').first()
        if not config:
            return
        candidate, token, before = config.bullet_candidate, config.bullet_validation_token, config.bullet_validation_base
        config.bullet_validation = 'CHECKING'
        config.bullet_validation_message = 'Checking generated trainer compatibility. The current revision remains active.'
        config.save(update_fields=['bullet_validation', 'bullet_validation_message'])
    try:
        message = check_revision(candidate, stop_event)
    except Exception as error:
        TrainingConfiguration.objects.filter(pk=1, bullet_validation_token=token, bullet_validation='CHECKING').update(
            bullet_validation='FAILED', bullet_validation_message=str(error)[-6500:])
        return
    with transaction.atomic():
        config = TrainingConfiguration.objects.select_for_update().get(pk=1)
        if config.bullet_validation_token != token or config.bullet_validation != 'CHECKING':
            return
        if config.bullet_commit != before:
            config.bullet_validation = 'FAILED'
            config.bullet_validation_message = 'The active revision changed during validation. Submit the revision again.'
        else:
            config.bullet_commit = candidate
            config.bullet_validation = 'PASSED'
            config.bullet_validation_message = message
        config.save(update_fields=['bullet_commit', 'bullet_validation', 'bullet_validation_message'])
