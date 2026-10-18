import hashlib
import json
import math
import os
import re
import shutil
import uuid

import requests
from django.conf import settings
from django.core.exceptions import ValidationError
from django.http import FileResponse, JsonResponse, StreamingHttpResponse
from django.utils import timezone
from django.views.decorators.http import require_POST

from OpenBench.models import WorkflowArtifact, WorkflowTask
from OpenBench.training_api import json_body, worker_endpoint
from OpenBench.training_storage import artifact_path, make_directory, storage_lock, sync_directory, verify_artifact
from OpenBench.workflow_contracts import parameter_values, ports


def owned(request, pk, active=True):
    task = WorkflowTask.objects.filter(pk=pk, worker=request.training_worker).first()
    if not task or active and (task.state != 'RUNNING' or task.cancel_requested):
        raise ValidationError('This workflow task claim is closed.')
    return task


@worker_endpoint
@require_POST
def control(request, pk):
    task = owned(request, pk, active=False)
    return JsonResponse({'state': task.state, 'stop': task.cancel_requested or task.state != 'RUNNING'})


@worker_endpoint
@require_POST
def report(request, pk):
    data = json_body(request)
    with storage_lock():
        task = owned(request, pk, active=False)
        sequence, state = data.get('sequence'), data.get('state')
        if type(sequence) is not int or sequence < 1:
            raise ValidationError('Invalid report sequence.')
        if sequence <= task.report_sequence or task.state != 'RUNNING':
            return JsonResponse({'sequence': task.report_sequence, 'state': task.state, 'stop': task.cancel_requested or task.state != 'RUNNING'})
        if state not in ('RUNNING', 'COMPLETED', 'FAILED', 'CANCELLED'):
            raise ValidationError('Invalid task state.')
        if task.cancel_requested and state not in ('FAILED', 'CANCELLED'):
            return JsonResponse({'stop': True, 'state': task.state})
        if state == 'COMPLETED' and set(task.outputs) != set(ports(task.snapshot['node'])[1]):
            raise ValidationError('Save all declared outputs before completing this task.')
        metrics, log, error = data.get('metrics', {}), data.get('log', ''), data.get('error', '')
        if not isinstance(metrics, dict) or len(metrics) > 64 or any(not isinstance(key, str) or len(key) > 64 or type(value) not in (int, float, str) or isinstance(value, str) and len(value) > 256 or type(value) in (int, float) and not math.isfinite(value) for key, value in metrics.items()):
            raise ValidationError('Invalid task metrics.')
        if not isinstance(log, str) or len(log) > 32768 or not isinstance(error, str) or len(error) > 2048:
            raise ValidationError('Invalid task log.')
        task.state, task.report_sequence, task.updated = state, sequence, timezone.now()
        task.metrics, task.log, task.error = {**task.metrics, **metrics}, (task.log + log)[-131072:], error
        if state != 'RUNNING':
            task.finished = task.updated
        task.save()
        return JsonResponse({'sequence': sequence, 'state': state, 'stop': state != 'RUNNING'})


def input_file(task, index):
    files = task.snapshot['files']
    if not 0 <= index < len(files):
        raise ValidationError('Unknown task input.')
    return files[index]


@worker_endpoint
def access(request, pk, file_index):
    task = owned(request, pk)
    file = input_file(task, file_index)
    if file.get('provider'):
        return JsonResponse({'proxy': True, 'size': file['size']})
    from huggingface_hub import get_hf_file_metadata, hf_hub_url
    from OpenBench.training import hf_token
    metadata = get_hf_file_metadata(hf_hub_url(file['repo'], file['path'], repo_type=file.get('repo_type', 'dataset'), revision=file['commit']), token=hf_token(task.owner_id))
    if metadata.size != file['size'] or metadata.commit_hash != file['commit']:
        raise ValidationError('Pinned input metadata changed.')
    return JsonResponse({'xet_hash': metadata.xet_file_data.file_hash, 'size': file['size']} if metadata.xet_file_data else {'proxy': True, 'size': file['size']})


@worker_endpoint
def download(request, pk, file_index):
    task = owned(request, pk)
    file = input_file(task, file_index)
    if file.get('provider') == 'network':
        from django.core.files.storage import FileSystemStorage
        return FileResponse(FileSystemStorage().open(file['network'], 'rb'), content_type='application/octet-stream')
    if file.get('provider'):
        from OpenBench.workflow_assets import local_file
        path = local_file(file, task.owner)
        if not path.is_file() or path.stat().st_size != file['size']:
            raise ValidationError('Pinned input file is missing or changed.')
        return FileResponse(path.open('rb'), content_type='application/octet-stream')
    from huggingface_hub import hf_hub_url
    from OpenBench.training import hf_token
    stream = requests.get(hf_hub_url(file['repo'], file['path'], repo_type=file.get('repo_type', 'dataset'), revision=file['commit']), headers={'Authorization': 'Bearer ' + hf_token(task.owner_id)}, stream=True, timeout=(15, 60))
    stream.raise_for_status()
    def chunks():
        try:
            yield from stream.iter_content(1024 ** 2)
        finally:
            stream.close()
    return StreamingHttpResponse(chunks(), content_type='application/octet-stream')


@worker_endpoint
def xet_token(request, pk):
    task = owned(request, pk)
    file = input_file(task, int(request.GET['file']))
    from OpenBench.training import hf_token
    response = requests.get('https://huggingface.co/api/%s/%s/xet-read-token/%s' % ('models' if file.get('repo_type') == 'model' else 'datasets', file['repo'], file['commit']), headers={'Authorization': 'Bearer ' + hf_token(task.owner_id)}, timeout=25)
    response.raise_for_status()
    token = response.json()
    result = JsonResponse({key: token[key] for key in ('accessToken', 'exp', 'casUrl')})
    for header, key in (('X-Xet-Cas-Url', 'casUrl'), ('X-Xet-Access-Token', 'accessToken'), ('X-Xet-Token-Expiration', 'exp')):
        result[header] = str(token[key])
    return result


@worker_endpoint
@require_POST
def upload(request, pk):
    task = owned(request, pk)
    name, digest = request.GET.get('name', ''), request.GET.get('sha256', '')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,255}', name) or not re.fullmatch(r'[a-f0-9]{64}', digest):
        raise ValidationError('Invalid output filename or checksum.')
    existing = task.artifacts.filter(name=name).first()
    if existing:
        if existing.sha256 != digest:
            raise ValidationError('This output name already contains another file.')
        verify_artifact(existing)
        return JsonResponse({'id': existing.pk, 'sha256': digest, 'size': existing.size})
    if task.artifacts.count() >= 4096:
        raise ValidationError('Task artifact limit reached.')
    directory = artifact_path('workflows/%d' % task.pk)
    make_directory(directory)
    temporary = directory / (uuid.uuid4().hex + '.part')
    result, size = hashlib.sha256(), 0
    try:
        with temporary.open('xb') as destination:
            while chunk := request.read(1024 ** 2):
                size += len(chunk)
                if size > settings.TRAINING_MAX_ARTIFACT_BYTES or shutil.disk_usage(directory).free < settings.TRAINING_STORAGE_RESERVE_BYTES + len(chunk):
                    raise ValidationError('Output exceeds the artifact size or storage limit.')
                result.update(chunk)
                destination.write(chunk)
                if size % (64 * 1024 ** 2) < 1024 ** 2:
                    WorkflowTask.objects.filter(pk=task.pk, state='RUNNING').update(updated=timezone.now())
            destination.flush()
            os.fsync(destination.fileno())
        if not size or result.hexdigest() != digest:
            raise ValidationError('Output checksum verification failed.')
        with storage_lock():
            owned(request, pk)
            existing = task.artifacts.filter(name=name).first()
            if existing:
                if existing.sha256 != digest or existing.size != size:
                    raise ValidationError('Another file already occupies this output name.')
                return JsonResponse({'id': existing.pk, 'sha256': digest, 'size': size})
            destination = directory / (digest + '-' + name)
            os.replace(temporary, destination)
            sync_directory(directory)
            row = WorkflowArtifact.objects.create(task=task, name=name, path=str(destination.relative_to(settings.TRAINING_ROOT)).replace('\\', '/'), sha256=digest, size=size)
            verify_artifact(row)
        return JsonResponse({'id': row.pk, 'sha256': digest, 'size': size})
    finally:
        temporary.unlink(missing_ok=True)


@worker_endpoint
@require_POST
def outputs(request, pk):
    value = json_body(request, limit=2 * 1024 ** 2)
    with storage_lock():
        task = owned(request, pk)
        declarations = ports(task.snapshot['node'])[1]
        if set(value) != set(declarations):
            raise ValidationError('Supply every declared output port.')
        clean = {}
        for port, kind in declarations.items():
            asset = value[port]
            if not isinstance(asset, dict) or asset.get('type') != kind:
                raise ValidationError('Output type differs from the declared port.')
            if kind in ('parameters', 'metrics'):
                values = asset.get('values')
                parameter_values(values)
                clean[port] = {'type': kind, 'values': values}
                continue
            descriptors = asset.get('files', [])
            if not isinstance(descriptors, list) or not descriptors or len(descriptors) > 4096:
                raise ValidationError('Output files are required.')
            files = []
            for file in descriptors:
                row = task.artifacts.filter(pk=file.get('artifact')).first()
                if not row:
                    raise ValidationError('An output file was not uploaded by this task.')
                verify_artifact(row)
                metadata = {'format': file.get('format'), 'shuffled': file.get('shuffled', False), 'interleaved': file.get('interleaved', False)}
                if kind == 'dataset' and (metadata['format'] not in ('pgn', 'vf', 'wf') or type(metadata['shuffled']) is not bool or type(metadata['interleaved']) is not bool):
                    raise ValidationError('Dataset outputs require format and ordering metadata.')
                files.append({'provider': 'workflow', 'artifact': row.pk, 'path': row.name, 'size': row.size, 'sha256': row.sha256, **metadata})
            provenance = {'workflow_task': task.pk}
            if kind == 'dataset':
                source = {'provider': 'workflow', 'repo': 'mattbench/task-%d' % task.pk, 'ref': str(task.pk), 'commit': str(task.pk), 'patterns': [file['path'] for file in files], 'files': files, 'size': sum(file['size'] for file in files)}
                clean[port] = {'type': kind, 'sources': [source], 'provenance': provenance}
            elif kind == 'network':
                from OpenBench.workflow_networks import register_network
                clean[port] = register_network(task, files)
            elif kind == 'checkpoint':
                raise ValidationError('Optimizer checkpoints must be registered through the training checkpoint protocol.')
            else:
                clean[port] = {'type': kind, 'files': files, 'provenance': provenance}
        task.outputs = clean
        task.updated = timezone.now()
        task.save(update_fields=['outputs', 'updated'])
    return JsonResponse({'stored': True})


def upload_context(request, pk):
    task = owned(request, pk)
    if task.snapshot['node']['kind'] != 'upload':
        raise ValidationError('This task is not an upload.')
    config = task.snapshot['node']['config']
    return task, config


def upload_descriptors(task, files):
    from OpenBench.dataset_manifest import validate_manifest
    validate_manifest({'version': 1, 'files': files})
    originals = task.snapshot['files']
    if len(files) != len(originals):
        raise ValidationError('Publish every input file.')
    for index, (file, original) in enumerate(zip(files, originals)):
        if not re.fullmatch(r'workflows/%d/%05d\.(?:vf|wf|pgn(?:\.tar|\.gz|\.bz2|\.zst)?|tar(?:\.gz|\.bz2|\.zst)?)' % (task.pk, index), file['path']) or file['size'] != original['size'] or original.get('sha256') and file['sha256'] != original['sha256'] or file['format'] != original['format'] or file['shuffled'] != original.get('shuffled', False) or file.get('interleaved', False) != original.get('interleaved', False):
            raise ValidationError('Published files must preserve the pinned inputs.')


@worker_endpoint
@require_POST
def upload_instruction(request, pk):
    task, config = upload_context(request, pk)
    data = json_body(request)
    index = data.get('index')
    if type(index) is not int or not 0 <= index < len(task.snapshot['files']):
        raise ValidationError('Invalid upload input.')
    original = task.snapshot['files'][index]
    if data.get('size') != original['size'] or not re.fullmatch(r'[a-f0-9]{64}', data.get('sha256', '')) or original.get('sha256') and data['sha256'] != original['sha256']:
        raise ValidationError('Upload checksum differs from the pinned input.')
    from OpenBench.training_datasets import upload_instruction as instruction
    return instruction(config['repo'], config.get('branch', 'main'), task.owner_id, data)


@worker_endpoint
@require_POST
def publish(request, pk):
    import base64
    from urllib.parse import quote
    from huggingface_hub import HfApi
    from OpenBench.dataset_manifest import read_manifest, resolve_dataset, validate_manifest
    from OpenBench.training import hf_token
    task, config = upload_context(request, pk)
    if task.outputs:
        return JsonResponse({'stored': True})
    files = json_body(request, limit=2 * 1024 ** 2).get('files')
    upload_descriptors(task, files)
    api = HfApi(token=hf_token(task.owner_id))
    info = api.dataset_info(config['repo'], revision=config.get('branch', 'main'), files_metadata=True)
    manifest = read_manifest(config['repo'], info, hf_token(task.owner_id))
    if manifest is None:
        from OpenBench.training import DATA_SUFFIXES
        manifest = resolve_dataset(config['repo'], info, ['*'], hf_token(task.owner_id))['manifest'] if any(file.rfilename.lower().endswith(DATA_SUFFIXES) for file in info.siblings) else {'version': 1, 'files': []}
    existing = {file['path']: file for file in manifest['files']}
    for file in files:
        if file['path'] in existing and existing[file['path']] != file:
            raise ValidationError('The destination path contains a different file.')
        existing[file['path']] = file
    manifest = validate_manifest({**manifest, 'files': list(existing.values())})
    lines = [{'key': 'header', 'value': {'summary': 'Publish workflow task %d' % task.pk, 'description': '', 'parentCommit': info.sha}}]
    lines.extend({'key': 'lfsFile', 'value': {'path': file['path'], 'algo': 'sha256', 'oid': file['sha256'], 'size': file['size']}} for file in files)
    lines.append({'key': 'file', 'value': {'path': 'dataset.json', 'encoding': 'base64', 'content': base64.b64encode(json.dumps(manifest).encode()).decode()}})
    response = requests.post('https://huggingface.co/api/datasets/%s/commit/%s' % (config['repo'], quote(config.get('branch', 'main'), safe='')), headers={'Authorization': 'Bearer ' + hf_token(task.owner_id), 'Content-Type': 'application/x-ndjson'}, data='\n'.join(json.dumps(line) for line in lines).encode(), timeout=(15, 60))
    response.raise_for_status()
    revision = response.json()['commitOid']
    asset = {'type': 'dataset', 'sources': [{'repo': config['repo'], 'ref': revision, 'commit': revision, 'patterns': [file['path'] for file in files]}], 'provenance': {'workflow_task': task.pk}}
    WorkflowTask.objects.filter(pk=task.pk, state='RUNNING', cancel_requested=False).update(outputs={'data': asset}, updated=timezone.now())
    return JsonResponse({'stored': True, 'revision': revision})


@worker_endpoint
@require_POST
def prepare_upload(request, pk):
    from huggingface_hub import HfApi
    from huggingface_hub.errors import RepositoryNotFoundError
    from OpenBench.training import hf_token
    task, config = upload_context(request, pk)
    api = HfApi(token=hf_token(task.owner_id))
    try:
        info = api.dataset_info(config['repo'], revision=config.get('branch', 'main'))
    except RepositoryNotFoundError:
        api.create_repo(config['repo'], repo_type='dataset', private=config.get('private', True), exist_ok=True)
        info = api.dataset_info(config['repo'], revision=config.get('branch', 'main'))
    if info.private != config.get('private', True):
        raise ValidationError('Destination visibility differs from the requested visibility.')
    return JsonResponse({'ready': True})
