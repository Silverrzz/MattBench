import hashlib
import os
import shutil
import uuid
from contextlib import contextmanager
from functools import wraps
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.management.base import CommandError
from django.db import transaction
from django.utils import timezone

from OpenBench.models import TrainingServiceLease


def offline_operation(operation):
    @wraps(operation)
    def wrapped(*args, **kwargs):
        from OpenBench.apps import acquire_watcher_lockfile
        if TrainingServiceLease.objects.filter(name__startswith='coordinator-', expires__gt=timezone.now()).exists():
            raise CommandError('Stop the training coordinator before backup or restore.')
        lock = acquire_watcher_lockfile()
        if lock is None:
            raise CommandError('Stop the web server before backup or restore so database and media remain consistent.')
        with lock:
            return operation(*args, **kwargs)
    return wrapped


def sync_directory(path):
    if os.name != 'nt':
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def sync_file(path):
    with Path(path).open('r+b') as source:
        os.fsync(source.fileno())
    sync_directory(Path(path).parent)


def make_directory(path):
    missing = []
    parent = Path(path)
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    Path(path).mkdir(parents=True, exist_ok=True)
    for created in reversed(missing):
        sync_directory(created.parent)


@contextmanager
def storage_lock():
    with transaction.atomic():
        row, _ = TrainingServiceLease.objects.get_or_create(name='artifact-storage', defaults={'token': uuid.uuid4(), 'expires': timezone.now()})
        TrainingServiceLease.objects.select_for_update().get(pk=row.pk)
        yield


def artifact_path(relative, replica=False):
    root = Path(settings.TRAINING_REPLICA_ROOT if replica else settings.TRAINING_ROOT).resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or path == root:
        raise ValidationError('Invalid artifact storage path.')
    return path


def valid_file(path, size, digest):
    if not path.is_file() or path.stat().st_size != size:
        return False
    result = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(4 * 1024 ** 2), b''):
            result.update(chunk)
    return result.hexdigest() == digest


def replicate(relative, size, digest):
    if not settings.TRAINING_REPLICA_ROOT:
        return False
    source = artifact_path(relative)
    destination = artifact_path(relative, replica=True)
    if valid_file(destination, size, digest):
        return True
    make_directory(destination.parent)
    if shutil.disk_usage(destination.parent).free < size + settings.TRAINING_STORAGE_RESERVE_BYTES:
        raise ValidationError('Artifact replica storage is below its free-space reserve.')
    temporary = destination.with_name(uuid.uuid4().hex + '.part')
    try:
        with source.open('rb') as incoming, temporary.open('xb') as output:
            shutil.copyfileobj(incoming, output, 4 * 1024 ** 2)
            output.flush()
            os.fsync(output.fileno())
        if not valid_file(temporary, size, digest):
            raise ValidationError('Artifact replica verification failed.')
        os.replace(temporary, destination)
        sync_directory(destination.parent)
        return True
    finally:
        temporary.unlink(missing_ok=True)


def verify_artifact(row):
    path = artifact_path(row.path)
    if not valid_file(path, row.size, row.sha256):
        raise ValidationError('Stored artifact is missing or corrupt. Reconcile storage before continuing.')
    return replicate(row.path, row.size, row.sha256)


def remove_artifact(relative):
    artifact_path(relative).unlink(missing_ok=True)
    if settings.TRAINING_REPLICA_ROOT:
        artifact_path(relative, replica=True).unlink(missing_ok=True)
