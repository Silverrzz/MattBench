import hashlib
import json
import math
import os
import re
import secrets
import shutil
import uuid
from datetime import timedelta
from functools import wraps
from pathlib import Path

import requests
import httpx
from django.conf import settings
from django.contrib.auth import authenticate
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import F, Q
from django.http import JsonResponse, StreamingHttpResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_POST

from OpenBench.models import Machine, Profile, TrainingArtifact, TrainingRun, TrainingWorker, TrainingWorkload
from OpenBench.training import download_access, hf_token, xet_access
from OpenBench.training import worker_requirement_errors
from OpenBench.training_models import TRAINING_ACTIVE, TRAINING_TERMINAL
from OpenBench.lifecycle import record_event
from OpenBench.training_storage import make_directory, storage_lock, sync_directory, verify_artifact


def json_body(request, limit=65536):
    if int(request.META.get('CONTENT_LENGTH') or 0) > limit:
        raise ValueError('Request is too large.')
    data = json.loads(request.body)
    if not isinstance(data, dict):
        raise ValueError('Expected a JSON object.')
    return data


def worker_endpoint(view):
    @csrf_exempt
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        try:
            key = request.headers.get('Authorization', '').removeprefix('Bearer ')
            worker_id = uuid.UUID(request.headers.get('X-Training-Worker', ''))
            worker = TrainingWorker.objects.select_related('owner').filter(pk=worker_id, enabled=True, owner__is_active=True).first()
            if not worker or not secrets.compare_digest(worker.secret_hash, hashlib.sha256(key.encode()).hexdigest()) or not Profile.objects.filter(user=worker.owner, enabled=True).exists():
                return JsonResponse({'error': 'Worker authentication failed.'}, status=403)
            request.training_worker = worker
            if worker.machine_id:
                Machine.objects.filter(pk=worker.machine_id).update(updated=timezone.now())
            response = view(request, *args, **kwargs)
        except (ValueError, TypeError, KeyError, IndexError, ValidationError) as error:
            message = '; '.join(error.messages) if isinstance(error, ValidationError) else 'Invalid worker request.'
            response = JsonResponse({'error': message}, status=400)
        except (requests.RequestException, httpx.HTTPError):
            response = JsonResponse({'error': 'Hugging Face is unavailable. Retry shortly.'}, status=503)
        response['Cache-Control'] = 'no-store'
        return response
    return wrapped


@csrf_exempt
@sensitive_post_parameters('password', 'token', 'machine_secret')
@require_POST
def register(request):
    machine = None
    if request.POST.get('machine_id'):
        machine = Machine.objects.select_related('user').filter(pk=request.POST['machine_id']).first()
        if not machine or not secrets.compare_digest(machine.secret, request.POST.get('machine_secret', '')):
            return JsonResponse({'error': 'Worker authentication failed.'}, status=403)
        user = machine.user
    else:
        user = authenticate(username=request.POST.get('username'), password=request.POST.get('password'))
    if not user or not user.is_active or not Profile.objects.filter(user=user, enabled=True).exists():
        return JsonResponse({'error': 'Enabled account credentials required.'}, status=403)
    try:
        info = json.loads(request.POST.get('info', '{}'))
        if machine and isinstance(info, dict):
            info['threads'] = machine.info['concurrency']
        if not isinstance(info, dict) or len(json.dumps(info)) > 8192:
            raise ValueError
        if info.get('protocol') not in (3, 4) or info.get('backend') not in ('cuda', 'rocm'):
            raise ValueError
        if info.get('protocol') == 4 and (not isinstance(info.get('capabilities'), list) or any(not isinstance(capability, str) for capability in info['capabilities'])):
            raise ValueError
        for field in ('vram_gb', 'disk_gb', 'threads'):
            if type(info.get(field)) not in (int, float) or not math.isfinite(info[field]) or info[field] <= 0:
                raise ValueError
        if not re.fullmatch(r'[a-f0-9]{64}', info.get('pawnocchio_sha256', '')):
            raise ValueError
        name = request.POST.get('name', '').strip()
        if not 1 <= len(name) <= 128:
            raise ValueError
        worker_id = uuid.UUID(request.POST.get('worker', ''))
        secret = request.POST.get('token', '')
        if not re.fullmatch(r'[A-Za-z0-9_-]{64}', secret):
            raise ValueError
        if not isinstance(info.get('runtime'), dict) or not info['runtime'].get('worker_sha256'):
            raise ValueError
        execution_image = info.get('execution_image', '')
        if not isinstance(execution_image, str) or execution_image and not re.fullmatch(r'[^\s]+@sha256:[a-f0-9]{64}', execution_image):
            raise ValueError
    except (ValueError, TypeError):
        return JsonResponse({'error': 'Invalid persistent worker identity.'}, status=400)
    with transaction.atomic():
        if machine:
            machine = Machine.objects.select_for_update().get(pk=machine.pk)
        mode = request.POST.get('mode', 'automatic')
        if mode not in ('automatic', 'testing-only', 'training-only', 'paused'):
            return JsonResponse({'error': 'Invalid worker mode.'}, status=400)
        worker, created = TrainingWorker.objects.get_or_create(pk=worker_id, defaults={'owner': user, 'name': name, 'info': info, 'secret_hash': hashlib.sha256(secret.encode()).hexdigest(), 'mode': machine.mode if machine else mode})
        if not created:
            if worker.owner_id != user.pk or not worker.enabled or not secrets.compare_digest(worker.secret_hash, hashlib.sha256(secret.encode()).hexdigest()):
                return JsonResponse({'error': 'Worker identity is revoked or belongs to another account.'}, status=403)
            if TrainingRun.objects.filter(worker=worker, state__in=TRAINING_ACTIVE).exists() and worker.info.get('runtime') != info.get('runtime'):
                return JsonResponse({'error': 'Finish or cancel the active training run before changing this worker runtime.'}, status=409)
            if worker.info.get('custom_name'):
                info['custom_name'] = worker.info['custom_name']
            worker.info = info
            worker.name = info.get('custom_name', name)
            worker.updated = timezone.now()
            worker.save(update_fields=['info', 'name', 'updated'])
        if machine:
            if not created:
                machine.mode = worker.mode
                machine.save(update_fields=['mode'])
            worker.machine = machine
            worker.save(update_fields=['machine'])
        record_event('worker.connected', worker, user.pk, {'backend': info['backend'], 'gpu': info.get('gpu', ''), 'registered': created})
    response = JsonResponse({'worker': str(worker.pk), 'token': secret, 'protocol': info['protocol'], 'capabilities': ['training-workloads', 'typed-assignments'] if info['protocol'] == 4 else []})
    response['Cache-Control'] = 'no-store'
    return response


@worker_endpoint
@require_POST
@transaction.atomic
def claim(request):
    worker = request.training_worker
    if worker.info.get('protocol') not in (3, 4):
        return JsonResponse({'error': 'Update and register the training worker to use dataset manifests.'}, status=409)
    machine = Machine.objects.select_for_update().get(pk=worker.machine_id) if worker.machine_id else None
    worker = TrainingWorker.objects.select_for_update().get(pk=worker.pk)
    data = json_body(request)
    if worker.test_assignment.get('claim_id') == data.get('claim_id') and (machine.mode if machine else worker.mode) != 'paused':
        return JsonResponse(worker.test_assignment['response'])
    if machine and data.get('machine_idle') is True and machine.workload == data.get('completed_workload'):
        Machine.objects.filter(pk=machine.pk).update(workload=0, mnps=0, dev_mnps=0, base_mnps=0)
        machine.workload = 0
    claim_id = uuid.UUID(data.get('claim_id', ''))
    now = timezone.now()
    free_disk = data.get('disk_gb', worker.info['disk_gb'])
    if type(free_disk) not in (int, float) or not math.isfinite(free_disk) or free_disk < 0:
        raise ValueError
    info = {**worker.info, 'disk_gb': free_disk}
    TrainingWorker.objects.filter(pk=worker.pk).update(updated=now, info=info)
    for attempt in worker.workloads.filter(state='ACTIVE', run__state__in=TRAINING_TERMINAL).select_related('run'):
        attempt.state, attempt.finished = attempt.run.state, now
        attempt.save(update_fields=['state', 'finished'])
    previous = TrainingWorkload.objects.filter(worker=worker, claim_token=claim_id).first()
    if previous and previous.state != 'ACTIVE':
        return JsonResponse({'run': None, 'claim_finished': True})
    existing = TrainingRun.objects.filter(worker=worker, claim_id=claim_id).first() or TrainingRun.objects.filter(worker=worker, state__in=TRAINING_ACTIVE).first()
    if existing:
        return JsonResponse(training_assignment(existing, info))
    if (machine.mode if machine else worker.mode) not in ('automatic', 'training-only') or machine and machine.workload:
        return JsonResponse({'run': None})
    candidates = TrainingRun.objects.filter(state='QUEUED', worker=None, cancel_requested=False, deleted=False).exclude(snapshot__has_key='demo')
    if not worker.accept_any_owner:
        candidates = candidates.filter(owner=worker.owner)
    if worker.info.get('protocol') < 4 or 'training-workloads' not in worker.info.get('capabilities', []):
        candidates = candidates.filter(workload_size=0)
    candidates = candidates.filter(Q(requested_worker=None) | Q(requested_worker=worker)).select_related('engine').order_by(F('last_allocated').asc(nulls_first=True), 'created', 'pk')
    from OpenBench.workloads.get_workload import machine_info_list, filter_valid_workloads, get_workload
    from OpenBench.training_workloads import refine_candidates
    preferences = machine.info if machine else info
    only = machine_info_list(preferences, 'only')
    if only:
        candidates = candidates.filter(engine__name__in=only)
    eligible = []
    for run in candidates:
        config = run.snapshot['settings']
        from OpenBench.dataset_manifest import required_disk_bytes
        disk_required = required_disk_bytes(run.dataset, config) / 1024 ** 3 + config.get('disk_reserve_gb', 10)
        if config.get('execution_image') and config['execution_image'] != info.get('execution_image'):
            continue
        if worker_requirement_errors(info, config, disk_required):
            continue
        eligible.append(run)
    request.workload_blacklist = data.get('blacklist', [])
    if not isinstance(request.workload_blacklist, list) or any(type(pk) is not int for pk in request.workload_blacklist):
        raise ValueError
    tests = filter_valid_workloads(request, machine, refine=False)[0] if machine and machine.mode == 'automatic' else []
    eligible, tests, preferred = refine_candidates(eligible, tests, preferences)
    if tests and (not eligible or worker.last_allocation_kind == 'training'):
        worker.last_allocation_kind = 'test'
        worker.save(update_fields=['last_allocation_kind'])
        if info.get('protocol') >= 4 and 'typed-assignments' in info.get('capabilities', []):
            result = get_workload(request, machine, tests, preferred)
            response = {'run': None, 'assignment': {'type': 'test', **result}, 'settings': machine.info.get('worker_settings', {})}
            worker.test_assignment = {'claim_id': str(claim_id), 'response': response}
            worker.save(update_fields=['test_assignment'])
            return JsonResponse(response)
        return JsonResponse({'run': None})
    for run in eligible:
        try:
            with transaction.atomic():
                run = TrainingRun.objects.select_for_update().get(pk=run.pk)
                if run.state != 'QUEUED' or run.worker_id or run.cancel_requested or run.deleted:
                    continue
                runtime = info.get('runtime', {})
                snapshot = {**run.snapshot, 'runtime': runtime, 'resume_semantics': 'optimiser-continuation; dataset reader restarts at the selected stage'}
                claimed = TrainingRun.objects.filter(pk=run.pk, state='QUEUED', worker=None, cancel_requested=False, deleted=False).update(worker=worker, claim_id=claim_id, snapshot=snapshot, state='DOWNLOADING', started=run.started or now, updated=now, last_allocated=now, error='')
                if claimed:
                    run.refresh_from_db()
                    if run.workload_size:
                        from OpenBench.training_workloads import claim_workload
                        claim_workload(run, worker, claim_id)
                    worker.last_allocation_kind = 'training'
                    worker.save(update_fields=['last_allocation_kind'])
                    if worker.machine_id:
                        Machine.objects.filter(pk=worker.machine_id).update(workload=0, updated=now)
                    record_event('training.started', run, run.owner_id, {'worker_id': str(worker.pk)})
        except IntegrityError:
            return JsonResponse({'run': None})
        if claimed:
            run.refresh_from_db()
            return JsonResponse(training_assignment(run, info))
    return JsonResponse({'run': None})


def training_assignment(run, info):
    job = assignment(run, info)
    return {'run': job, 'assignment': {'type': 'training', 'run': job}}


def assignment(run, info):
    from OpenBench.training_datasets import effective_dataset
    from OpenBench.dataset_manifest import required_disk_bytes
    snapshot = run.snapshot
    workload_data = None
    if run.workload_size:
        workload = run.workloads.get(state='ACTIVE')
        snapshot = dict(snapshot)
        if workload.resume_checkpoint_id:
            from OpenBench.training_checkpoints import checkpoint_provenance
            snapshot['resume'] = checkpoint_provenance(workload.resume_checkpoint)
        workload_data = {'id': workload.pk, 'claim_token': str(workload.claim_token), 'start': workload.start, 'end': workload.end, 'report_sequence': workload.report_sequence, 'finalize_only': bool(workload.resume_checkpoint_id and workload.resume_checkpoint.superbatch == workload.end)}
    return {'id': run.pk, 'name': run.name, 'engine': run.engine.name, 'snapshot': snapshot, 'dataset': effective_dataset(run), 'parameters': run.parameters, 'worker_info': info, 'state': run.state, 'report_sequence': run.report_sequence, 'cancel_requested': run.cancel_requested or run.deleted, 'recovery_pending': bool(run.metrics.get('recovery_pending')), 'workload': workload_data, 'priority': run.priority, 'completed_superbatches': run.completed_superbatches, 'required_disk_bytes': required_disk_bytes(run.dataset, run.snapshot['settings']) + run.snapshot['settings'].get('disk_reserve_gb', 10) * 1024 ** 3}


@worker_endpoint
@require_POST
def recover(request, pk):
    from OpenBench.training_checkpoints import resume_training
    with storage_lock():
        run = TrainingRun.objects.select_for_update().get(pk=owned_run(request, pk).pk)
        if run.workload_size:
            from OpenBench.training_workloads import expire_workload
            expire_workload(run, 'Worker restarted; continuing from the latest committed checkpoint.')
            return JsonResponse({'recovery_run': run.pk})
        if run.recovery_run_id:
            return JsonResponse({'recovery_run': run.recovery_run_id})
        if run.deleted or run.state in ('COMPLETED', 'CANCELLED'):
            return JsonResponse({'recovery_run': None})
        if run.terminal and not run.metrics.get('recovery_pending') and not run.error.startswith(('Worker heartbeat lost.', 'Worker restarted.')):
            return JsonResponse({'recovery_run': None})
        if not run.terminal:
            run.state = 'CANCELLED' if run.cancel_requested else 'FAILED'
            run.finished = run.updated = timezone.now()
            run.error = 'Worker restarted. Local files were preserved.'
            run.save(update_fields=['state', 'finished', 'updated', 'error'])
            record_event('training.cancelled' if run.cancel_requested else 'training.interrupted', run, run.owner_id,
                         {'reason': 'worker_restarted', 'recovery_pending': not run.cancel_requested})
        if not run.cancel_requested:
            if run.snapshot['settings'].get('resume_supported') and run.checkpoints.exists():
                resumed = resume_training(run.owner, run)
            else:
                from OpenBench.training_datasets import effective_dataset
                if run.snapshot.get('resume'):
                    if not run.resume_from_id:
                        raise ValidationError('The starting checkpoint was removed. Create a new train from an available checkpoint.')
                    verify_artifact(run.resume_from.archive)
                resumed = TrainingRun.objects.create(owner=run.owner, engine=run.engine, name=run.name, schedule=run.schedule, snapshot=run.snapshot, dataset=effective_dataset(run), parameters=run.parameters, resume_from=run.resume_from, state='QUEUED')
                record_event('training.created', resumed, resumed.owner_id)
                record_event('training.queued', resumed, resumed.owner_id)
            resumed.snapshot = {**resumed.snapshot, 'automatic_recovery': True, 'recovery_source': run.pk}
            resumed.save(update_fields=['snapshot'])
            run.recovery_run = resumed
            run.save(update_fields=['recovery_run'])
        if run.recovery_run_id:
            record_event('training.recovered', run, run.owner_id, {'recovery_run': run.recovery_run_id})
        return JsonResponse({'recovery_run': run.recovery_run_id})


def owned_run(request, pk):
    run = TrainingRun.objects.filter(pk=pk, worker=request.training_worker).first()
    if not run:
        raise ValidationError('This run is not assigned to this worker.')
    from OpenBench.training_workloads import check_claim
    check_claim(request, run)
    return run


@worker_endpoint
@require_POST
def control(request, pk):
    try:
        run = owned_run(request, pk)
    except ValidationError:
        return JsonResponse({'stop': True, 'state': 'EXPIRED'})
    return JsonResponse({'stop': run.cancel_requested or run.deleted or run.terminal, 'state': run.state})


@worker_endpoint
@require_POST
def report(request, pk):
    data = json_body(request)
    from OpenBench.training_workloads import completion_retry, complete_workload
    if retry := completion_retry(request, pk, data):
        return JsonResponse(retry)
    try:
        run = owned_run(request, pk)
    except ValidationError:
        if retry := completion_retry(request, pk, data):
            return JsonResponse(retry)
        raise
    if run.workload_size and data.get('state') == 'COMPLETED':
        return JsonResponse(complete_workload(request, run, data))
    if run.terminal:
        return JsonResponse({'stop': True, 'state': run.state, 'sequence': run.report_sequence})
    if (run.cancel_requested or run.deleted) and data.get('state') not in ('CANCELLED', 'FAILED'):
        return JsonResponse({'stop': True, 'state': run.state})
    sequence = data.get('sequence')
    if type(sequence) is not int or sequence < 1:
        raise ValueError
    if sequence <= run.report_sequence:
        return JsonResponse({'stop': run.cancel_requested or run.deleted, 'sequence': run.report_sequence})
    state = data.get('state', run.state)
    if state != run.state and state not in ('FAILED', 'CANCELLED'):
        order = list(TRAINING_ACTIVE) + ['COMPLETED']
        if run.state not in order or state not in order or order.index(state) != order.index(run.state) + 1:
            raise ValidationError('Invalid training stage transition.')
    if state == 'COMPLETED':
        artifacts = run.artifacts.all()
        if run.deleted or run.cancel_requested or not artifacts.filter(kind='network').exists() or not artifacts.filter(kind='manifest').exists() or not artifacts.filter(kind='log').exists():
            raise ValidationError('Networks, manifest and complete log must be saved before completing a run.')
    metrics = data.get('metrics', {})
    if not isinstance(metrics, dict) or len(metrics) > 32:
        raise ValueError
    clean_metrics = dict(run.metrics)
    for key, value in metrics.items():
        if not re.fullmatch(r'[a-z][a-z0-9_]{0,39}', key):
            raise ValueError
        if type(value) in (int, float) and math.isfinite(value) and abs(value) <= 1e20:
            clean_metrics[key] = value
        elif key == 'current_file' and isinstance(value, str) and len(value) <= 256:
            clean_metrics[key] = value
        else:
            raise ValueError
    if 'progress' in clean_metrics:
        clean_metrics['progress'] = max(0, min(100, float(clean_metrics['progress'])))
    log = data.get('log', '')
    error = data.get('error', '')
    if not isinstance(log, str) or not isinstance(error, str) or len(log) > 32768 or len(error) > 2048:
        raise ValueError
    log = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', log)
    history = run.history
    now = timezone.now()
    if state == 'TRAINING' or run.state == 'TRAINING':
        from OpenBench.training_telemetry import bullet_telemetry, merge_loss_history
        context = run.log_tail
        if not clean_metrics.get('end_superbatch'):
            path = Path(settings.TRAINING_ROOT) / str(pk) / 'worker.log'
            if path.is_file():
                with path.open(encoding='utf-8', errors='replace') as source:
                    context = source.read(65536) + '\n' + context
        clean_metrics, samples = bullet_telemetry(context + log, clean_metrics)
        if state == 'SAVING':
            clean_metrics['progress'] = metrics.get('progress', 0)
        if 'loss' in metrics and not samples and clean_metrics.get('superbatch'):
            samples.append({'loss': metrics['loss'], 'step': clean_metrics['superbatch'], 'superbatch': clean_metrics['superbatch']})
        history = merge_loss_history(history, samples, now.timestamp())
    from OpenBench.training_telemetry import training_metrics
    clean_metrics = training_metrics(clean_metrics, run.snapshot, run.dataset, state)
    if run.workload_size and state != 'TRAINING':
        from OpenBench.training_workloads import run_end
        clean_metrics['phase_progress'] = clean_metrics.get('progress', 0)
        completed = max(run.completed_superbatches, clean_metrics.get('superbatch', 0)) if state == 'SAVING' else run.completed_superbatches
        clean_metrics['progress'] = 100 * min(completed, run_end(run)) / run_end(run)
    changes = {'state': state, 'updated': now, 'metrics': clean_metrics, 'history': history, 'log_tail': (run.log_tail + log)[-32768:], 'report_sequence': sequence}
    if error:
        changes['error'] = error
    if state in TRAINING_TERMINAL:
        changes['finished'] = now
    if state == 'COMPLETED':
        from OpenBench.training_checkpoints import prune_checkpoints
        with storage_lock():
            prune_checkpoints(run)
    with transaction.atomic():
        locked = TrainingRun.objects.select_for_update().get(pk=run.pk)
        from OpenBench.training_workloads import check_claim
        workload = check_claim(request, locked)
        changed = TrainingRun.objects.filter(pk=pk, claim_id=run.claim_id, worker=run.worker, report_sequence=run.report_sequence, state=run.state, cancel_requested=run.cancel_requested, deleted=run.deleted).update(**changes)
        if changed and workload:
            workload.report_sequence = sequence
            if state in ('FAILED', 'CANCELLED'):
                workload.state, workload.finished = state, now
            workload.save(update_fields=['report_sequence', 'state', 'finished'])
        if changed and state != run.state:
            previous_state = run.state
            run.refresh_from_db()
            record_event('training.' + state.lower(), run, run.owner_id, {'from': previous_state, 'to': state, 'worker_id': str(request.training_worker.pk)}, key='training.state:%d:%d' % (run.pk, sequence))
            if state == 'FAILED' and clean_metrics.get('recovery_pending') and not run.workload_size:
                record_event('training.interrupted', run, run.owner_id)
    if changed:
        TrainingWorker.objects.filter(pk=request.training_worker.pk).update(updated=now)
        if log:
            directory = Path(settings.TRAINING_ROOT) / str(pk)
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / 'worker.log'
            if not path.exists() or path.stat().st_size < 64 * 1024 ** 2:
                with path.open('a', encoding='utf-8') as output:
                    output.write(log)
        if workload and state == 'FAILED' and clean_metrics.get('recovery_pending'):
            from OpenBench.training_workloads import expire_workload
            expire_workload(run, 'Worker interrupted; continuing from the latest committed checkpoint.')
    return JsonResponse({'stop': run.cancel_requested or run.deleted, 'state': state if changed else run.state, 'sequence': sequence if changed else run.report_sequence})


@worker_endpoint
def dataset_access(request, pk, file_index):
    run = owned_run(request, pk)
    if run.state not in ('DOWNLOADING', 'CONVERTING') or run.cancel_requested or run.deleted:
        return JsonResponse({'error': 'Dataset access is closed for this run.'}, status=403)
    return JsonResponse(download_access(run, file_index, request.GET.get('stage')))


@worker_endpoint
def xet_token(request, pk):
    run = owned_run(request, pk)
    if run.state not in ('DOWNLOADING', 'CONVERTING') or run.cancel_requested or run.deleted:
        return JsonResponse({'error': 'Dataset access is closed for this run.'}, status=403)
    file_index = request.GET.get('file')
    if file_index is not None:
        try:
            file_index = int(file_index)
            from OpenBench.training_datasets import dataset_input
            if not 0 <= file_index < len(dataset_input(run, request.GET.get('stage'))['files']):
                raise ValueError
        except ValueError:
            raise ValidationError('Invalid dataset file.')
    result = xet_access(run, file_index, request.GET.get('stage'))
    response = JsonResponse(result)
    response['X-Xet-Cas-Url'] = result['casUrl']
    response['X-Xet-Access-Token'] = result['accessToken']
    response['X-Xet-Token-Expiration'] = str(result['exp'])
    return response


@worker_endpoint
def dataset_file(request, pk, file_index):
    from huggingface_hub import hf_hub_url
    from OpenBench.training_datasets import dataset_input
    run = owned_run(request, pk)
    if run.state not in ('DOWNLOADING', 'CONVERTING') or run.cancel_requested or run.deleted:
        return JsonResponse({'error': 'Dataset access is closed for this run.'}, status=403)
    dataset = dataset_input(run, request.GET.get('stage'))
    file = dataset['files'][file_index]
    if file['size'] > 32 * 1024 ** 2:
        return JsonResponse({'error': 'Large files must use Hugging Face Xet or LFS storage.'}, status=400)
    url = hf_hub_url(file.get('repo', dataset['repo']), file['path'], repo_type='dataset', revision=file.get('commit', dataset['commit']))
    source = requests.get(url, headers={'Authorization': 'Bearer ' + hf_token(run.owner_id)}, stream=True, timeout=60)
    source.raise_for_status()
    def chunks():
        try:
            yield from source.iter_content(1024 * 1024)
        finally:
            source.close()
    return StreamingHttpResponse(chunks(), content_type='application/octet-stream')


@worker_endpoint
@require_POST
def upload_artifact(request, pk):
    run = owned_run(request, pk)
    if run.state not in ('TRAINING', 'SAVING') or run.cancel_requested or run.deleted:
        return JsonResponse({'error': 'This run is not accepting artifacts.'}, status=409)
    name = request.GET.get('name', '')
    kind = request.GET.get('kind', '')
    expected = request.GET.get('sha256', '')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', name) or not re.fullmatch(r'[a-f0-9]{64}', expected) or kind not in ('network', 'log', 'manifest', 'checkpoint'):
        raise ValueError
    workload = run.workloads.filter(state='ACTIVE').first() if run.workload_size else None
    if workload:
        name = '%s-%s' % (workload.claim_token.hex, name)
        if len(name) > 128:
            raise ValidationError('Artifact name is too long for a workload.')
    existing = TrainingArtifact.objects.filter(run=run, name=name).first()
    if existing:
        if existing.sha256 != expected or existing.kind != kind:
            return JsonResponse({'error': 'An artifact with this name already exists.'}, status=409)
        with storage_lock():
            replicated = verify_artifact(existing)
        return JsonResponse({'id': existing.pk, 'sha256': existing.sha256, 'size': existing.size, 'replicated': replicated})
    if run.artifacts.count() >= 100000:
        raise ValidationError('The run artifact limit was reached.')
    maximum = settings.TRAINING_MAX_ARTIFACT_BYTES
    if kind == 'network':
        maximum = min(maximum, run.snapshot['settings']['network_max_bytes'])
    if int(request.META.get('CONTENT_LENGTH') or 0) > maximum:
        raise ValidationError('Artifact exceeds the configured size limit.')
    directory = Path(settings.TRAINING_ROOT) / str(pk) / 'artifacts'
    make_directory(directory)
    temporary = directory / (uuid.uuid4().hex + '.part')
    digest = hashlib.sha256()
    size = 0
    try:
        with temporary.open('xb') as output:
            while chunk := request.read(1024 * 1024):
                if shutil.disk_usage(directory).free < settings.TRAINING_STORAGE_RESERVE_BYTES + len(chunk):
                    raise ValidationError('Artifact storage is below its free-space reserve.')
                size += len(chunk)
                if size > maximum:
                    raise ValidationError('Artifact exceeds the configured size limit.')
                digest.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        if digest.hexdigest() != expected:
            raise ValidationError('Artifact checksum does not match.')
        if not size or kind == 'network' and size < run.snapshot['settings']['network_min_bytes']:
            raise ValidationError('The artifact is empty or smaller than the expected network size.')
        if not TrainingRun.objects.filter(pk=pk, state__in=('TRAINING', 'SAVING'), cancel_requested=False, deleted=False).exists():
            raise ValidationError('The run stopped accepting artifacts.')
        destination = directory / (uuid.uuid4().hex + '-' + name)
        with storage_lock():
            locked = TrainingRun.objects.select_for_update().get(pk=run.pk)
            from OpenBench.training_workloads import check_claim
            check_claim(request, locked)
            if locked.state not in ('TRAINING', 'SAVING') or locked.cancel_requested or locked.deleted:
                raise ValidationError('The run stopped accepting artifacts.')
            temporary.rename(destination)
            sync_directory(destination.parent)
            try:
                with transaction.atomic():
                    row = TrainingArtifact.objects.create(run=run, workload=workload, name=name, kind=kind, sha256=expected, size=size, path=destination.relative_to(settings.TRAINING_ROOT).as_posix())
            except IntegrityError:
                destination.unlink(missing_ok=True)
                row = TrainingArtifact.objects.get(run=run, name=name)
                if row.sha256 != expected or row.kind != kind or row.size != size:
                    raise ValidationError('Artifact name conflict.')
            replicated = verify_artifact(row)
        return JsonResponse({'id': row.pk, 'sha256': expected, 'size': size, 'replicated': replicated})
    finally:
        temporary.unlink(missing_ok=True)
