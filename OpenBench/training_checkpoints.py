from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import FileResponse, JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_POST

from OpenBench.lifecycle import record_event
from OpenBench.models import Network, TrainingArtifact, TrainingCheckpoint, TrainingRun
from OpenBench.training_api import json_body, owned_run, worker_endpoint
from OpenBench.training_models import TRAINING_TERMINAL
from OpenBench.training_storage import storage_lock, verify_artifact, remove_artifact


@worker_endpoint
@require_POST
def checkpoint_ready(request, pk):
    run = owned_run(request, pk)
    data = json_body(request)
    superbatch = data.get('superbatch')
    metadata = data.get('metadata', {})
    if type(superbatch) is not int or not 1 <= superbatch <= 10000000 or not isinstance(metadata, dict) or len(str(metadata)) > 16384:
        raise ValidationError('Invalid checkpoint metadata.')
    with storage_lock():
        run = TrainingRun.objects.select_for_update().get(pk=run.pk)
        existing = TrainingCheckpoint.objects.filter(run=run, superbatch=superbatch).first()
        if existing:
            if existing.archive_id != data.get('archive') or existing.network_id != data.get('network'):
                raise ValidationError('This superbatch already has a different checkpoint.')
            replicated = verify_artifact(existing.archive) and verify_artifact(existing.network)
            return JsonResponse({'checkpoint': existing.pk, 'superbatch': existing.superbatch, 'stored': True, 'replicated': replicated})
        if run.state not in ('TRAINING', 'SAVING') or run.cancel_requested:
            raise ValidationError('This run is not accepting checkpoints.')
        archive = get_object_or_404(TrainingArtifact, pk=data.get('archive'), run=run, kind='checkpoint')
        network = get_object_or_404(TrainingArtifact, pk=data.get('network'), run=run, kind='network')
        archive_replicated = verify_artifact(archive)
        network_replicated = verify_artifact(network)
        checkpoint = TrainingCheckpoint.objects.create(run=run, superbatch=superbatch, archive=archive, network=network, metadata=metadata)
        record_event('training.checkpoint.saved', checkpoint, run.owner_id, {'run_id': run.pk, 'superbatch': superbatch, 'archive_sha256': archive.sha256, 'network_sha256': network.sha256})
        prune_checkpoints(run, checkpoint.pk)
    return JsonResponse({'checkpoint': checkpoint.pk, 'superbatch': superbatch, 'stored': True, 'replicated': archive_replicated and network_replicated})


def prune_checkpoints(run, newest_id=None):
    keep = run.snapshot.get('settings', {}).get('checkpoint_keep_last', 0)
    if type(keep) is not int or keep <= 0:
        return
    paths = []
    with transaction.atomic():
        TrainingRun.objects.select_for_update().get(pk=run.pk)
        candidates = list(run.checkpoints.select_for_update().select_related('archive').order_by('-superbatch')[keep:])
        protected = set(TrainingRun.objects.filter(resume_from__in=candidates).exclude(state__in=TRAINING_TERMINAL).values_list('resume_from_id', flat=True))
        protected.add(newest_id)
        removed = []
        for checkpoint in candidates:
            if checkpoint.pk in protected:
                continue
            TrainingRun.objects.filter(resume_from=checkpoint, state__in=TRAINING_TERMINAL).update(resume_from=None)
            archive = checkpoint.archive
            removed.append(checkpoint.superbatch)
            checkpoint.delete()
            paths.append(Path(settings.TRAINING_ROOT) / archive.path)
            archive.delete()
        if not removed:
            return
        record_event('training.checkpoints.pruned', run, run.owner_id, {'superbatches': removed, 'keep_latest': keep, 'network_exports_preserved': True}, key='training.prune:%s:%s' % (run.pk, max(removed)))
        def remove_files():
            root = Path(settings.TRAINING_ROOT).resolve()
            for path in paths:
                if path.resolve().is_relative_to(root):
                    remove_artifact(path.relative_to(root).as_posix())
        transaction.on_commit(remove_files)


@worker_endpoint
def resume_download(request, pk):
    run = owned_run(request, pk)
    if not run.resume_from_id or run.terminal or run.cancel_requested:
        raise ValidationError('No checkpoint is available for this run.')
    artifact = run.resume_from.archive
    response = FileResponse((Path(settings.TRAINING_ROOT) / artifact.path).open('rb'), as_attachment=True, filename=artifact.name)
    response['X-Checksum-SHA256'] = artifact.sha256
    response['Content-Length'] = str(artifact.size)
    return response


def resume_training(user, source, checkpoint_id=None, worker=None):
    from OpenBench.training_datasets import effective_dataset
    if not source.terminal:
        raise ValidationError('Stop the current run before resuming it on another worker.')
    if not source.snapshot['settings'].get('resume_supported'):
        raise ValidationError('This schedule has not enabled the checkpoint resume contract.')
    if checkpoint_id is not None and (not str(checkpoint_id).isdigit() or len(str(checkpoint_id)) > 18):
        raise ValidationError('Invalid checkpoint selection.')
    if worker and (worker.owner_id != user.pk or not worker.enabled):
        raise ValidationError('Choose an enabled worker belonging to this account.')
    with storage_lock():
        checkpoints = source.checkpoints.select_for_update().select_related('archive')
        checkpoint = checkpoints.filter(pk=checkpoint_id).first() if checkpoint_id else checkpoints.first()
        if not checkpoint:
            raise ValidationError('No complete checkpoint is available.')
        verify_artifact(checkpoint.archive)
        provenance = {'checkpoint_id': checkpoint.pk, 'run_id': source.pk, 'superbatch': checkpoint.superbatch, 'sha256': checkpoint.archive.sha256, 'size': checkpoint.archive.size, 'metadata': checkpoint.metadata}
        run = TrainingRun.objects.create(owner=user, engine=source.engine, name=source.name, schedule=source.schedule, snapshot={**source.snapshot, 'resume': provenance}, dataset=effective_dataset(source), parameters=source.parameters, requested_worker=worker, resume_from=checkpoint, state='QUEUED')
        record_event('training.resumed', run, user.pk, {'source_run_id': source.pk, **provenance})
        return run


def cleanup_checkpoints(run):
    if not run.terminal:
        raise ValidationError('Finish or stop training before cleaning up its checkpoints.')
    paths = []
    with transaction.atomic():
        checkpoints = list(run.checkpoints.select_for_update().select_related('archive', 'network'))
        if TrainingRun.objects.filter(resume_from__in=checkpoints).exclude(state__in=TRAINING_TERMINAL).exists():
            raise ValidationError('A running training still needs one of these checkpoints. Finish or cancel it before cleanup.')
        TrainingRun.objects.filter(resume_from__in=checkpoints, state__in=TRAINING_TERMINAL).update(resume_from=None)
        for checkpoint in checkpoints:
            archive = checkpoint.archive
            network = checkpoint.network
            checkpoint.delete()
            paths.append(Path(settings.TRAINING_ROOT) / archive.path)
            archive.delete()
            if not network.network_id:
                paths.append(Path(settings.TRAINING_ROOT) / network.path)
                network.delete()
        from django.db.models import Q
        for artifact in run.artifacts.filter(Q(kind='checkpoint') | Q(kind='network', name__startswith='sb-'), network=None):
            paths.append(Path(settings.TRAINING_ROOT) / artifact.path)
            artifact.delete()
        record_event('training.checkpoints.cleaned', run, run.owner_id, {'removed_files': len(paths)}, key='training.cleanup:%s:%s' % (run.pk, run.artifacts.count()))
        def remove_files():
            root = Path(settings.TRAINING_ROOT).resolve()
            for path in paths:
                if path.resolve().is_relative_to(root):
                    remove_artifact(path.relative_to(root).as_posix())
        transaction.on_commit(remove_files)


def import_checkpoint_networks(run, user, selected):
    import re
    from django.core.files import File
    from django.core.files.storage import FileSystemStorage
    from OpenBench.training import digest_file
    if not run.terminal:
        raise ValidationError('Finish or stop training before importing checkpoint networks.')
    if len(run.engine.name) > 64:
        raise ValidationError('Network storage requires an engine name of at most 64 characters.')
    selected = set(str(value) for value in selected)
    if any(not value.isdigit() or len(value) > 18 for value in selected):
        raise ValidationError('Invalid checkpoint selection.')
    rows = list(run.checkpoints.select_related('network').filter(pk__in=selected))
    if len(rows) != len(selected):
        raise ValidationError('Select checkpoints belonging to this training run.')
    storage = FileSystemStorage()
    for checkpoint in rows:
        artifact = checkpoint.network
        if artifact.network_id:
            continue
        suffix = '-r%d-sb%d' % (run.pk, checkpoint.superbatch)
        name = re.sub(r'[^A-Za-z0-9_.-]', '_', run.name)[:64 - len(suffix)] + suffix
        sha = artifact.sha256[:8].upper()
        with transaction.atomic():
            existing = Network.objects.filter(engine=run.engine.name, sha256=sha).first()
            if storage.exists(sha):
                if digest_file(storage.path(sha)) != artifact.sha256:
                    raise ValidationError('A short network hash collision prevented import.')
            else:
                with (Path(settings.TRAINING_ROOT) / artifact.path).open('rb') as source:
                    saved = storage.save(sha, File(source))
                if saved != sha:
                    storage.delete(saved)
                    raise ValidationError('A concurrent network upload used this hash. Try again.')
            if existing:
                network = existing
            else:
                if Network.objects.filter(engine=run.engine.name, name=name).exists():
                    raise ValidationError('A network with this checkpoint name already exists.')
                network = Network.objects.create(name=name, sha256=sha, engine=run.engine.name, author=user.username)
            TrainingArtifact.objects.filter(pk=artifact.pk).update(network=network)
            record_event('network.registered', network, user.pk, {'training_run_id': run.pk, 'checkpoint_id': checkpoint.pk})


def finish_checkpoints(run, user, selected):
    with storage_lock():
        checkpoints = list(run.checkpoints.select_for_update())
        if TrainingRun.objects.filter(resume_from__in=checkpoints).exclude(state__in=TRAINING_TERMINAL).exists():
            raise ValidationError('A running training still needs one of these checkpoints. Finish or cancel it before cleanup.')
        import_checkpoint_networks(run, user, selected)
        cleanup_checkpoints(run)
