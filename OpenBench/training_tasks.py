import hashlib
import json
import logging
import time
import threading
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import close_old_connections, transaction
from django.db.models import F
from django.utils import timezone

from OpenBench.models import DatasetUpload, TrainingRun
from OpenBench.training import hf_token, resolve_inputs
from OpenBench.training_models import TRAINING_ACTIVE
from OpenBench.lifecycle import record_event


logger = logging.getLogger(__name__)


def failure_message(error, operation):
    if isinstance(error, ValidationError):
        return '; '.join(error.messages)[:2048]
    status = getattr(getattr(error, 'response', None), 'status_code', None)
    if status in (401, 403):
        return '%s: access denied. Check your Hugging Face token and repository permissions.' % operation
    if status == 404:
        return '%s: repository, file or revision was not found.' % operation
    if status == 429:
        return '%s: the provider rate limit was reached. Try again later.' % operation
    return '%s failed (%s). Check the repository, revision, server connectivity and disk space, then try again.' % (operation, type(error).__name__)


def upload_dataset(task):
    from huggingface_hub import HfApi, CommitOperationAdd
    from OpenBench.dataset_manifest import MANIFEST_PATH, resolve_dataset
    from huggingface_hub.errors import RepositoryNotFoundError
    from OpenBench.dataset_upload_metadata import analyse_archive, upload_metadata
    api = HfApi(token=hf_token(task.owner_id))
    source = Path(settings.MEDIA_ROOT) / 'PGNs' / ('%d.pgn.tar' % task.workload_id)
    size = source.stat().st_size
    digest = hashlib.sha256()
    blob = hashlib.sha1(('blob %d\0' % size).encode())
    read = 0
    last = timezone.now()
    with source.open('rb') as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            digest.update(chunk)
            blob.update(chunk)
            read += len(chunk)
            if (timezone.now() - last).total_seconds() >= 2:
                last = timezone.now()
                DatasetUpload.objects.filter(pk=task.pk).update(stage='Checking archive', progress=100 * read / max(1, size), updated=last)
    sha256 = digest.hexdigest()
    DatasetUpload.objects.filter(pk=task.pk).update(stage='Analysing PGN archive', progress=0, updated=timezone.now())
    def analysis_progress():
        DatasetUpload.objects.filter(pk=task.pk).update(updated=timezone.now())
    statistics = analyse_archive(source, analysis_progress)
    if source.stat().st_size != size:
        raise ValidationError('The PGN archive changed while reading it. Wait for all datagen workers to finish.')
    DatasetUpload.objects.filter(pk=task.pk).update(sha256=sha256, stage='Connecting to Hugging Face', progress=0, updated=timezone.now())
    try:
        info = api.dataset_info(task.repo, files_metadata=True)
    except RepositoryNotFoundError:
        api.create_repo(task.repo, repo_type='dataset', private=task.private, exist_ok=True)
        info = api.dataset_info(task.repo, files_metadata=True)
    if info.private != task.private:
        raise ValidationError('The existing repository has a different visibility. Select its current visibility or use a new repository.')
    existing = api.get_paths_info(task.repo, paths=[task.filename], repo_type='dataset', revision=info.sha)
    operations = []
    if existing:
        if not ((getattr(existing[0], 'lfs', None) and existing[0].lfs.sha256 == sha256) or (not getattr(existing[0], 'lfs', None) and getattr(existing[0], 'blob_id', None) == blob.hexdigest())):
            raise ValidationError('That Hugging Face path already exists. Choose another filename to preserve the existing file.')
    else:
        DatasetUpload.objects.filter(pk=task.pk).update(stage='Uploading archive and dataset metadata', progress=0, updated=timezone.now())
        operations.append(CommitOperationAdd(path_in_repo=task.filename, path_or_fileobj=str(source)))
    from OpenBench.dataset_manifest import read_manifest
    from OpenBench.training import DATA_SUFFIXES
    manifest = read_manifest(task.repo, info, hf_token(task.owner_id))
    if manifest is None:
        if any(file.rfilename.lower().endswith(DATA_SUFFIXES) for file in info.siblings):
            resolved = resolve_dataset(task.repo, info, ['*'], hf_token(task.owner_id))
            manifest = {**resolved['manifest'], 'files': resolved['files']}
        else:
            manifest = {'version': 1, 'files': []}
    entry = {'path': task.filename, 'size': size, 'sha256': sha256, 'format': 'pgn', 'shuffled': False}
    manifest, reports = upload_metadata(manifest, entry, statistics, task.repo, info.sha, task.workload_id)
    DatasetUpload.objects.filter(pk=task.pk).update(stage='Uploading archive and dataset metadata', progress=0, updated=timezone.now())
    for path, content in reports.items():
        operations.append(CommitOperationAdd(path_in_repo=path, path_or_fileobj=content.encode('utf-8')))
    operations.append(CommitOperationAdd(path_in_repo=MANIFEST_PATH, path_or_fileobj=(json.dumps(manifest, indent=2) + '\n').encode('utf-8')))
    commit = api.create_commit(repo_id=task.repo, repo_type='dataset', operations=operations, parent_commit=info.sha, commit_message='Upload MattBench datagen %d' % task.workload_id)
    revision = commit.oid
    with transaction.atomic():
        DatasetUpload.objects.filter(pk=task.pk).update(state='COMPLETED', stage='Published', progress=100, revision=revision, updated=timezone.now())
        record_event('datagen.uploaded', task, task.owner_id, {'repo': task.repo, 'revision': revision, 'workload_id': task.workload_id})


class TrainingTasks:
    def __init__(self, stop_event):
        self.stop_event = stop_event
        self.active_started = None

    def run_lane(self, lane):
        if lane == 'validation':
            TrainingRun.objects.filter(state='PREPARING').exclude(snapshot__has_key='demo').update(state='VALIDATING')
        elif lane == 'uploads':
            DatasetUpload.objects.filter(state='UPLOADING').exclude(workload__execution__has_key='demo').update(state='QUEUED', stage='Retrying after coordinator restart')
        operation = {'validation': self.validate, 'uploads': self.upload, 'maintenance': self.maintain}[lane]
        while not self.stop_event.is_set():
            close_old_connections()
            try:
                self.active_started = time.monotonic()
                operation()
            except Exception:
                logger.exception('Training coordinator %s failed', lane)
            finally:
                self.active_started = None
            self.stop_event.wait(5)

    def failed(self, task, error, expected, retry, operation):
        status = getattr(getattr(error, 'response', None), 'status_code', None)
        transient = not isinstance(error, ValidationError) and status not in (400, 401, 403, 404)
        state = retry if transient and task.task_attempts < 5 else 'FAILED'
        now = timezone.now()
        changes = {'state': state, 'error': failure_message(error, operation), 'updated': now}
        if isinstance(task, TrainingRun) and state == 'FAILED':
            changes['finished'] = now
        with transaction.atomic():
            if type(task).objects.filter(pk=task.pk, state=expected).update(**changes):
                record_event('training.task.' + ('retry' if state == retry else 'failed'), task, task.owner_id, {'operation': operation, 'attempt': task.task_attempts}, key='task:%s:%s:%s:%s' % (type(task).__name__, task.pk, operation, task.task_attempts))
        logger.warning('%s %s: %s', operation, task.pk, changes['error'])
        if state == retry:
            self.stop_event.wait(min(300, 10 * 2 ** task.task_attempts))

    def validate(self):
        run = TrainingRun.objects.filter(state='VALIDATING', deleted=False).exclude(snapshot__has_key='demo').order_by('created').first()
        if not run:
            return
        if run.task_attempts >= 5:
            TrainingRun.objects.filter(pk=run.pk, state='VALIDATING', deleted=False).update(state='FAILED', error='Input validation exceeded five attempts. Check coordinator logs before restarting.', finished=timezone.now(), updated=timezone.now())
            return
        if not TrainingRun.objects.filter(pk=run.pk, state='VALIDATING', deleted=False).update(state='PREPARING', task_attempts=F('task_attempts') + 1, updated=timezone.now()):
            return
        run.refresh_from_db()
        try:
            snapshot, dataset = resolve_inputs(run)
            with transaction.atomic():
                if TrainingRun.objects.filter(pk=run.pk, state='PREPARING', cancel_requested=False).update(snapshot=snapshot, dataset=dataset, state='QUEUED', error='', updated=timezone.now()):
                    record_event('training.queued', run, run.owner_id, {'dataset_revision': dataset['commit'], 'bullet_commit': snapshot['bullet_commit']})
        except Exception as error:
            self.failed(run, error, 'PREPARING', 'VALIDATING', 'Input validation')

    def upload(self):
        task = DatasetUpload.objects.filter(state='QUEUED').exclude(workload__execution__has_key='demo').order_by('created').first()
        if not task:
            return
        if task.task_attempts >= 5:
            DatasetUpload.objects.filter(pk=task.pk, state='QUEUED').update(state='FAILED', error='Upload exceeded five attempts. Check coordinator logs before retrying.', updated=timezone.now())
            return
        if not DatasetUpload.objects.filter(pk=task.pk, state='QUEUED').update(state='UPLOADING', task_attempts=F('task_attempts') + 1, stage='Checking archive', error='', updated=timezone.now()):
            return
        task.refresh_from_db()
        try:
            upload_dataset(task)
        except Exception as error:
            self.failed(task, error, 'UPLOADING', 'QUEUED', 'Dataset upload')

    def maintain(self):
        now = timezone.now()
        cutoff = now - timedelta(seconds=settings.TRAINING_WORKER_TIMEOUT)
        for stale in TrainingRun.objects.filter(state__in=TRAINING_ACTIVE, updated__lt=cutoff).exclude(snapshot__has_key='demo'):
            with transaction.atomic():
                if TrainingRun.objects.filter(pk=stale.pk, state__in=TRAINING_ACTIVE, updated__lt=cutoff).update(state='FAILED', error='Worker heartbeat lost. Training will recover automatically when the worker reconnects.', metrics={**stale.metrics, 'recovery_pending': 1}, finished=now, updated=now):
                    record_event('training.interrupted', stale, stale.owner_id, {'latest_checkpoint_id': stale.checkpoints.values_list('pk', flat=True).first()})
