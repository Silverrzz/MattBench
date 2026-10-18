import copy
import re
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.utils import timezone

from OpenBench.models import TrainingRun, WorkflowTask
from OpenBench.workflow_contracts import ports


def validate(node):
    config = node['config']
    if config.get('accelerator', 'cpu') not in ('cpu', 'cuda', 'rocm'):
        raise ValidationError('Choose CPU, CUDA or ROCm for this task.')
    if node['kind'] != 'task':
        return
    commands = config.get('commands')
    if not isinstance(commands, list) or not 1 <= len(commands) <= 64:
        raise ValidationError('Add one or more command argument arrays.')
    for command in commands:
        if not isinstance(command, list) or not 1 <= len(command) <= 128 or any(not isinstance(arg, str) or not arg or len(arg) > 4096 or '\0' in arg for arg in command):
            raise ValidationError('Commands must be arrays of arguments.')
    repository = config.get('repository', {})
    if repository and (not isinstance(repository, dict) or not re.fullmatch(r'https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository.get('url', '')) or not re.fullmatch(r'[a-f0-9]{40}', repository.get('commit', ''))):
        raise ValidationError('Pin the task repository to a full GitHub commit.')
    image = config.get('execution_image', '')
    if not isinstance(image, str) or image and not re.fullmatch(r'[^\s]+@sha256:[a-f0-9]{64}', image):
        raise ValidationError('Pin the execution image by SHA256.')
    outputs = ports(node)[1]
    if 'checkpoint' in outputs.values():
        raise ValidationError('Use a training node to produce optimizer checkpoints.')
    if 'network' in outputs.values() and not config.get('engine'):
        raise ValidationError('Choose an engine for network outputs.')
    files = config.get('files', {})
    if not isinstance(files, dict) or set(files) != set(outputs):
        raise ValidationError('Choose an output file pattern for every output port.')
    from OpenBench.training import relative_path
    for value in files.values():
        relative_path(value)


def asset_files(asset):
    if isinstance(asset, list):
        return [file for item in asset for file in asset_files(item)]
    if asset.get('type') == 'dataset':
        return [file for source in asset['sources'] for file in source.get('files', [])]
    return asset.get('files', [])


def enqueue(step, node, inputs):
    from OpenBench.workflow_assets import resolve_data
    prepared = copy.deepcopy(inputs)
    for name, value in prepared.items():
        values = value if isinstance(value, list) else [value]
        for index, asset in enumerate(values):
            if asset.get('type') == 'dataset':
                values[index] = resolve_data(asset, step.run.owner)
            elif asset.get('type') in ('network', 'checkpoint'):
                from OpenBench.models import Network, TrainingCheckpoint
                from django.core.files.storage import FileSystemStorage
                if asset['type'] == 'checkpoint':
                    row = TrainingCheckpoint.objects.select_related('archive', 'run').get(pk=asset['checkpoint'])
                    if row.run.owner_id != step.run.owner_id and not step.run.owner.is_superuser:
                        raise ValidationError('Checkpoint access denied.')
                    values[index]['files'] = [{'provider': 'training', 'artifact': row.archive_id, 'path': row.archive.name, 'sha256': row.archive.sha256, 'size': row.archive.size}]
                else:
                    row = Network.objects.get(sha256=asset['network'])
                    storage = FileSystemStorage()
                    import hashlib
                    with storage.open(row.sha256, 'rb') as stream:
                        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
                    values[index]['files'] = [{'provider': 'network', 'network': row.sha256, 'path': row.sha256, 'size': storage.size(row.sha256), 'sha256': digest}]
        prepared[name] = values if isinstance(value, list) else values[0]
    files = []
    seen = set()
    for asset in prepared.values():
        for file in asset_files(asset):
            identity = (file.get('provider', 'huggingface'), file.get('repo'), file.get('commit'), file.get('artifact'), file.get('workload'), file['path'])
            if identity not in seen:
                files.append(file)
                seen.add(identity)
    if len(files) > 4096:
        raise ValidationError('A task accepts at most 4096 files.')
    step.task = WorkflowTask.objects.create(owner=step.run.owner, snapshot={'workflow': {'run': str(step.run_id), 'step': step.key}, 'worker_policy': node['worker'], 'node': node, 'files': files}, inputs=prepared)


def assignment(task, info):
    from OpenBench.worker_policy import allocation
    from OpenBench.training import DEFAULT_SETTINGS
    from OpenBench.training_profiles import profile_settings
    selected = allocation(info, task.snapshot.get('worker_policy', {}), info['threads'])
    if selected is None:
        raise ValidationError('This worker cannot satisfy the workflow CPU allocation.')
    profile = task.snapshot['node']['config'].get('training_profile', {})
    return {'run': None, 'assignment': {'type': 'workflow-task', 'task': {'id': task.pk, 'name': task.snapshot['node']['name'], 'state': task.state,
        'snapshot': {'settings': {**DEFAULT_SETTINGS, **(profile_settings(profile) if profile else {}), 'max_hours': task.snapshot['node']['timeout_hours'], 'disk_reserve_gb': 2, 'threads': selected['threads'], 'execution_image': task.snapshot['node']['config'].get('execution_image', '')}},
        'specification': task.snapshot['node'], 'inputs': task.inputs, 'dataset': {'files': task.snapshot['files'], 'size': sum(file['size'] for file in task.snapshot['files'])},
        'worker_info': {**info, **selected}, 'report_sequence': task.report_sequence, 'cancel_requested': task.cancel_requested}}}


def claim(worker, machine, claim_id, info):
    if 'workflow-assets-v1' not in info.get('capabilities', []):
        return None
    existing = WorkflowTask.objects.filter(worker=worker, claim_id=claim_id).first() or WorkflowTask.objects.filter(worker=worker, state='RUNNING').first()
    if existing:
        return assignment(existing, info) if existing.state == 'RUNNING' else {'run': None, 'claim_finished': True}
    if (machine.mode if machine else worker.mode) not in ('automatic', 'training-only') or machine and machine.workload or TrainingRun.objects.filter(worker=worker, state__in=('DOWNLOADING', 'CONVERTING', 'COMPILING', 'TRAINING', 'SAVING')).exists():
        return None
    from OpenBench.worker_policy import eligible
    for task in WorkflowTask.objects.filter(state='QUEUED', owner=worker.owner, cancel_requested=False).order_by('created')[:100]:
        if not eligible(task, machine=machine, worker=worker):
            continue
        config = task.snapshot['node']['config']
        if config.get('accelerator', 'cpu') != 'cpu' and config['accelerator'] != info.get('backend'):
            continue
        if config.get('execution_image') and config['execution_image'] != info.get('execution_image'):
            continue
        size = sum(file['size'] for file in task.snapshot['files'])
        factor = 10 if task.snapshot['node']['kind'] in ('convert', 'split') else 2
        if size * factor + 2 * 1024 ** 3 > info['disk_gb'] * 1024 ** 3:
            continue
        if WorkflowTask.objects.filter(pk=task.pk, state='QUEUED', worker=None, cancel_requested=False).update(worker=worker, claim_id=claim_id, state='RUNNING', updated=timezone.now()):
            task.refresh_from_db()
            return assignment(task, info)
    return None


def expire():
    now = timezone.now()
    WorkflowTask.objects.filter(state='RUNNING', updated__lt=now - timedelta(minutes=5)).update(state='FAILED', error='Worker heartbeat lost. Partial files were preserved.', finished=now)
