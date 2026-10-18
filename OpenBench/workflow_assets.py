import copy
import fnmatch
import hashlib
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError

from OpenBench.models import Network, Test, TrainingArtifact, TrainingCheckpoint, TrainingDataset
from OpenBench.workflow_contracts import parameter_values, parse_parameters


def owned(query, user):
    return query if user.is_superuser else query.filter(owner=user)


def dataset_source(config, user):
    provider = config.get('provider', 'huggingface')
    if provider == 'registry':
        row = owned(TrainingDataset.objects.filter(pk=config.get('dataset'), archived=False), user).first()
        if not row:
            raise ValidationError('Choose an available dataset.')
        source = row.snapshot()
    elif provider == 'datagen':
        row = Test.objects.filter(pk=config.get('workload'), test_mode='DATAGEN', deleted=False).first()
        if not row or not user.is_superuser and row.author != user.username:
            raise ValidationError('Choose one of your datagen workloads.')
        source = {'provider': 'datagen', 'workload': row.pk}
    elif provider == 'huggingface':
        from OpenBench.training import repo_id
        patterns = config.get('patterns', ['*'])
        if isinstance(patterns, str):
            patterns = [line.strip() for line in patterns.splitlines() if line.strip()]
        if not isinstance(patterns, list) or not 1 <= len(patterns) <= 256 or any(not isinstance(item, str) or not item or len(item) > 512 for item in patterns):
            raise ValidationError('Choose dataset file patterns.')
        source = {'repo': repo_id(config.get('repo', '')), 'ref': config.get('ref') or 'main', 'patterns': patterns}
        if not isinstance(source['ref'], str) or len(source['ref']) > 200:
            raise ValidationError('Choose a valid dataset revision.')
    else:
        raise ValidationError('Choose a supported dataset source.')
    return {'type': 'dataset', 'sources': [source]}


def parameters_source(config, user):
    if config.get('provider', 'inline') == 'inline':
        return {'type': 'parameters', 'values': parse_parameters(config.get('values', '')), 'provenance': {'source': 'inline'}}
    row = Test.objects.filter(pk=config.get('workload'), test_mode='SPSA', deleted=False).first()
    if not row or not user.is_superuser and row.author != user.username:
        raise ValidationError('Choose one of your SPSA workloads.')
    return parameters_from_tune(row)


def parameters_from_tune(row):
    values = {item.name: item.value if item.is_float else int(round(item.value)) for item in row.spsa_run.parameters.order_by('index')}
    return {'type': 'parameters', 'values': parameter_values(values), 'provenance': {'workload': row.pk, 'games': row.games}}


def network_source(config):
    row = Network.objects.filter(sha256=config.get('network')).first()
    if not row:
        raise ValidationError('Choose an available network.')
    return {'type': 'network', 'network': row.sha256, 'network_name': row.name, 'engine': row.engine}


def checkpoint_source(config, user):
    row = TrainingCheckpoint.objects.select_related('run', 'archive', 'network').filter(pk=config.get('checkpoint')).first()
    if not row or not user.is_superuser and row.run.owner_id != user.pk:
        raise ValidationError('Choose an available checkpoint.')
    from OpenBench.training_checkpoints import checkpoint_provenance
    from OpenBench.training_storage import verify_artifact
    verify_artifact(row.archive)
    verify_artifact(row.network)
    return {'type': 'checkpoint', 'checkpoint': row.pk, 'training': row.run_id, 'provenance': checkpoint_provenance(row)}


def training_artifact(config, user):
    if config.get('provider') == 'huggingface':
        from huggingface_hub import HfApi
        from OpenBench.training import hf_token, relative_path, repo_id
        repository = repo_id(config.get('repo', ''))
        path = relative_path(config.get('path', ''))
        kind = config.get('repo_type', 'dataset')
        if kind not in ('dataset', 'model'):
            raise ValidationError('Choose a dataset or model repository.')
        info = HfApi(token=hf_token(user.pk)).repo_info(repository, repo_type=kind, revision=config.get('ref') or 'main', files_metadata=True)
        entry = next((file for file in info.siblings if file.rfilename == path), None)
        if not entry or not entry.size:
            raise ValidationError('The selected repository file is missing or empty.')
        return {'type': 'artifact', 'files': [{'repo': repository, 'repo_type': kind, 'commit': info.sha, 'path': path, 'size': entry.size, 'sha256': entry.lfs.sha256 if entry.lfs else '', 'blob_id': entry.blob_id}]}
    if config.get('provider') == 'workflow':
        from OpenBench.models import WorkflowArtifact
        row = WorkflowArtifact.objects.select_related('task').filter(pk=config.get('artifact')).first()
        if not row or not user.is_superuser and row.task.owner_id != user.pk:
            raise ValidationError('Choose one of your workflow artifacts.')
        return {'type': 'artifact', 'files': [{'provider': 'workflow', 'artifact': row.pk, 'path': row.name, 'sha256': row.sha256, 'size': row.size}]}
    row = TrainingArtifact.objects.select_related('run').filter(pk=config.get('artifact')).first()
    if not row or not user.is_superuser and row.run.owner_id != user.pk:
        raise ValidationError('Choose an available training artifact.')
    from OpenBench.training_storage import verify_artifact
    verify_artifact(row)
    return {'type': 'artifact', 'files': [{'provider': 'training', 'artifact': row.pk, 'path': row.name, 'sha256': row.sha256, 'size': row.size}], 'provenance': {'training': row.run_id}}


def resolve_source(source, user):
    provider = source.get('provider', 'huggingface')
    if provider == 'datagen':
        row = Test.objects.filter(pk=source['workload'], deleted=False, test_mode='DATAGEN').first()
        if not row or not user.is_superuser and row.author != user.username:
            raise ValidationError('The datagen source is unavailable.')
        from OpenBench.utils import getRecentMachines
        path = Path(settings.MEDIA_ROOT) / 'PGNs' / ('%d.pgn.tar' % row.pk)
        if not row.finished or getRecentMachines().filter(workload=row.pk).exists() or not path.is_file():
            raise ValidationError('Wait for the datagen workload and its archive to finish.')
        if source.get('files'):
            files = source['files']
            if len(files) != 1 or path.stat().st_size != files[0]['size']:
                raise ValidationError('The pinned datagen archive has changed.')
        else:
            with path.open('rb') as stream:
                digest = hashlib.file_digest(stream, 'sha256').hexdigest()
            files = [{'provider': 'datagen', 'workload': row.pk, 'path': path.name, 'size': path.stat().st_size, 'sha256': digest, 'format': 'pgn', 'shuffled': False}]
        return {**source, 'repo': 'mattbench/datagen-%d' % row.pk, 'commit': files[0]['sha256'], 'ref': files[0]['sha256'], 'patterns': [file['path'] for file in files], 'files': files, 'size': sum(file['size'] for file in files)}
    if provider == 'workflow':
        from OpenBench.models import WorkflowArtifact
        files = []
        for entry in source['files']:
            row = WorkflowArtifact.objects.select_related('task').filter(pk=entry['artifact']).first()
            if not row or not user.is_superuser and row.task.owner_id != user.pk or row.sha256 != entry['sha256'] or row.size != entry['size']:
                raise ValidationError('The workflow dataset artifact is unavailable.')
            files.append(copy.deepcopy(entry))
        return {**source, 'files': files, 'size': sum(file['size'] for file in files)}
    if provider != 'huggingface':
        raise ValidationError('Unsupported dataset provider.')
    from huggingface_hub import HfApi
    from OpenBench.training import hf_token
    from OpenBench.dataset_manifest import resolve_dataset
    token = hf_token(user.pk)
    info = HfApi(token=token).dataset_info(source['repo'], revision=source.get('commit') or source['ref'], files_metadata=True)
    metadata = resolve_dataset(source['repo'], info, source['patterns'], token)
    allowed = {file['path'] for file in source['files']} if source.get('files') else None
    files = [{**file, 'repo': source['repo'], 'commit': info.sha} for file in metadata['files'] if allowed is None or file['path'] in allowed]
    if allowed is not None and allowed != {file['path'] for file in files}:
        raise ValidationError('Pinned dataset files are missing.')
    return {**source, **{key: value for key, value in metadata.items() if key != 'files'}, 'ref': info.sha, 'commit': info.sha, 'files': files, 'size': sum(file['size'] for file in files)}


def resolve_data(asset, user):
    if asset.get('type') != 'dataset' or not isinstance(asset.get('sources'), list):
        raise ValidationError('A dataset asset is required.')
    sources = [resolve_source(source, user) for source in asset['sources']]
    return {**asset, 'sources': sources}


def select_data(asset, patterns):
    if isinstance(patterns, str):
        patterns = patterns.splitlines()
    if not isinstance(patterns, list) or not patterns or any(not isinstance(item, str) or len(item) > 512 for item in patterns):
        raise ValidationError('Supply file selection patterns.')
    selected, remaining = [], []
    for source in asset['sources']:
        matching = [file for file in source['files'] if any(fnmatch.fnmatchcase(file['path'], pattern) for pattern in patterns)]
        other = [file for file in source['files'] if file not in matching]
        for target, files in ((selected, matching), (remaining, other)):
            if files:
                target.append({**source, 'files': files, 'patterns': [file['path'] for file in files], 'size': sum(file['size'] for file in files)})
    return {'type': 'dataset', 'sources': selected}, {'type': 'dataset', 'sources': remaining}


def dataset_for_training(asset):
    sources = asset['sources']
    if not sources:
        raise ValidationError('The selected dataset is empty.')
    return sources[0] if len(sources) == 1 else {'sources': sources}


def local_file(file, user):
    provider = file.get('provider')
    if provider == 'datagen':
        row = Test.objects.filter(pk=file['workload'], author=user.username, test_mode='DATAGEN').first()
        if not row and not user.is_superuser:
            raise ValidationError('Datagen file access denied.')
        return Path(settings.MEDIA_ROOT) / 'PGNs' / ('%d.pgn.tar' % file['workload'])
    from OpenBench.models import WorkflowArtifact
    query = TrainingArtifact.objects.select_related('run') if provider == 'training' else WorkflowArtifact.objects.select_related('task')
    row = query.filter(pk=file.get('artifact')).first()
    owner_id = row.run.owner_id if row and provider == 'training' else row.task.owner_id if row else None
    if not row or not user.is_superuser and owner_id != user.pk or row.sha256 != file['sha256'] or row.size != file['size']:
        raise ValidationError('Artifact file access denied.')
    from OpenBench.training_storage import artifact_path
    return artifact_path(row.path)
