"""Transactional allocation and completion of sequential training workloads."""
import json

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from OpenBench.models import TrainingRun, TrainingWorkload
from OpenBench.schedule_builder import MANIFEST
from OpenBench.training_storage import storage_lock, verify_artifact


def run_end(run):
    return json.loads(run.snapshot['files'][MANIFEST])['spec']['superbatches']


def claim_workload(run, worker, token):
    # The caller holds the run and worker locks. Checkpoints are registered only
    # after both files have been committed and verified.
    checkpoint = run.checkpoints.order_by('-superbatch').first() or run.resume_from
    start = checkpoint.superbatch + 1 if checkpoint else 1
    end = min(run_end(run), start + run.workload_size - 1)
    if start > end:
        # A new attempt can finish uploading its log/manifest after the final
        # checkpoint without training the already committed final SB again.
        start = end = run_end(run)
    workload = TrainingWorkload.objects.create(run=run, worker=worker, claim_token=token, start=start, end=end, resume_checkpoint=checkpoint)
    run.continuation_checkpoint = checkpoint
    run.completed_superbatches = checkpoint.superbatch if checkpoint else 0
    run.metrics = {**run.metrics, 'workload_start': start, 'workload_end': end, 'queued_continuation': 0, 'recovery_pending': 0}
    run.save(update_fields=['continuation_checkpoint', 'completed_superbatches', 'metrics'])
    return workload


def check_claim(request, run, *, active=True):
    if not run.workload_size:
        return None
    token = request.headers.get('X-Training-Claim', '')
    if not token or token != str(run.claim_id):
        raise ValidationError('This workload claim has expired.')
    workload = TrainingWorkload.objects.filter(run=run, worker=request.training_worker, claim_token=run.claim_id).first()
    if not workload or active and workload.state != 'ACTIVE':
        raise ValidationError('This workload is no longer active.')
    return workload


def completion_retry(request, pk, data):
    """A committed completion remains acknowledgeable after the run is requeued."""
    token = request.headers.get('X-Training-Claim', '')
    if not token or data.get('state') != 'COMPLETED':
        return None
    import uuid
    try:
        token = uuid.UUID(token)
    except ValueError:
        raise ValidationError('Invalid workload claim.')
    workload = TrainingWorkload.objects.filter(run_id=pk, worker=request.training_worker, claim_token=token, state='COMPLETED').first()
    if workload and data.get('sequence') == workload.report_sequence:
        return {'stop': False, 'state': 'COMPLETED', 'sequence': workload.report_sequence}
    return None


def complete_workload(request, run, data):
    with storage_lock():
        run = TrainingRun.objects.select_for_update().get(pk=run.pk)
        if retry := completion_retry(request, run.pk, data):
            return retry
        workload = check_claim(request, run)
        sequence = data.get('sequence')
        if type(sequence) is not int or sequence <= run.report_sequence:
            raise ValidationError('Completion requires a new report sequence.')
        if run.cancel_requested or run.deleted or run.state != 'SAVING':
            raise ValidationError('The workload is not ready to complete.')
        checkpoint = run.checkpoints.filter(superbatch=workload.end).select_related('archive', 'network').first()
        if checkpoint and checkpoint.pk != workload.resume_checkpoint_id and (checkpoint.archive.workload_id != workload.pk or checkpoint.network.workload_id != workload.pk):
            raise ValidationError('The boundary checkpoint belongs to another attempt.')
        if not checkpoint:
            raise ValidationError('A complete boundary checkpoint must be stored before releasing the worker.')
        for kind in ('log', 'manifest'):
            artifact = workload.artifacts.filter(kind=kind).first()
            if not artifact:
                raise ValidationError('The workload log and manifest must be stored before completion.')
            verify_artifact(artifact)
        verify_artifact(checkpoint.archive)
        verify_artifact(checkpoint.network)
        now = timezone.now()
        workload.state, workload.finished = 'COMPLETED', now
        workload.report_sequence, workload.checkpoint = sequence, checkpoint
        workload.save(update_fields=['state', 'finished', 'report_sequence', 'checkpoint'])
        final = workload.end == run_end(run)
        run.state = 'COMPLETED' if final else 'QUEUED'
        run.finished = now if final else None
        run.updated = now
        run.report_sequence = sequence
        run.continuation_checkpoint = checkpoint
        run.completed_superbatches = workload.end
        run.worker = None
        run.claim_id = None
        run.metrics = {**run.metrics, 'superbatch': workload.end, 'progress': 100 * workload.end / run_end(run), 'queued_continuation': int(not final)}
        run.save(update_fields=['state', 'finished', 'updated', 'report_sequence', 'continuation_checkpoint', 'completed_superbatches', 'worker', 'claim_id', 'metrics'])
        from OpenBench.lifecycle import record_event
        record_event('training.workload.completed', run, run.owner_id, {'workload': workload.pk, 'start': workload.start, 'end': workload.end}, key='training.workload.completed:%s' % workload.pk)
        from OpenBench.training_checkpoints import prune_checkpoints
        prune_checkpoints(run, checkpoint.pk)
        return {'stop': False, 'state': 'COMPLETED', 'sequence': sequence, 'continuation': not final}


def expire_workload(run, reason='Worker heartbeat lost.', cutoff=None):
    """Fence the attempt before exposing the run for another allocation."""
    with transaction.atomic():
        run = TrainingRun.objects.select_for_update().get(pk=run.pk)
        if cutoff and run.updated >= cutoff:
            return
        workloads = run.workloads.filter(state='ACTIVE')
        if run.state == 'FAILED' and run.metrics.get('recovery_pending'):
            workloads = run.workloads.filter(claim_token=run.claim_id, state__in=('ACTIVE', 'FAILED'))
        if not workloads.exists():
            return
        now = timezone.now()
        cancelled = run.cancel_requested or run.deleted
        checkpoint = run.checkpoints.order_by('-superbatch').first() or run.resume_from
        workloads.update(state='CANCELLED' if cancelled else 'EXPIRED', finished=now)
        run.state = 'CANCELLED' if cancelled else 'QUEUED'
        run.worker = None
        run.claim_id = None
        run.continuation_checkpoint = checkpoint
        run.completed_superbatches = checkpoint.superbatch if checkpoint else 0
        run.finished = now if cancelled else None
        run.updated = now
        run.error = reason
        run.metrics = {**run.metrics, 'recovery_pending': 0, 'queued_continuation': int(not cancelled), 'superbatch': run.completed_superbatches, 'progress': 100 * run.completed_superbatches / run_end(run)}
        run.save(update_fields=['state', 'worker', 'claim_id', 'continuation_checkpoint', 'completed_superbatches', 'finished', 'updated', 'error', 'metrics'])


def refine_candidates(training, tests, info):
    from OpenBench.workloads.get_workload import machine_info_list
    choices = [('training', run, run.engine.name) for run in training] + [('test', test, test.dev_engine) for test in tests]
    forces = machine_info_list(info, 'force')
    focuses = machine_info_list(info, 'focus') + machine_info_list(info, 'only')
    forced = [choice for choice in choices if choice[2] in forces]
    if forced:
        choices = forced
    if not choices:
        return [], [], False
    priority = max(choice[1].priority for choice in choices)
    choices = [choice for choice in choices if choice[1].priority == priority]
    focused = [choice for choice in choices if choice[2] in focuses]
    if focused:
        choices = focused
    return ([row for kind, row, _ in choices if kind == 'training'],
            [row for kind, row, _ in choices if kind == 'test'], bool(forced or focused))
