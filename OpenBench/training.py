import hashlib
import json
import re
from pathlib import Path, PurePosixPath
from urllib.parse import quote, urlsplit

import requests
from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.exceptions import ValidationError

from OpenBench.models import HuggingFaceCredential


DEFAULT_SETTINGS = {
    'bullet_repo': 'https://github.com/jw1912/bullet',
    'bullet_ref': 'main',
    'build': ['cargo', 'build', '--release', '--manifest-path', 'crates/bullet_lib/Cargo.toml', '--example', 'mattbench', '--features', 'cuda'],
    'run': ['target/release/examples/mattbench'],
    'example_name': 'mattbench',
    'example_manifest': 'crates/bullet_lib/Cargo.toml',
    'example_source': 'examples/mattbench.rs',
    'backend': 'cuda',
    'threads': 4,
    'min_vram_gb': 8,
    'min_disk_gb': 100,
    'max_hours': 72,
    'network_glob': '**/quantised.bin',
    'network_min_bytes': 64,
    'network_max_bytes': 1073741824,
    'skip_broken_games': False,
    'fill_missing_evals': '',
    'delete_uploaded_checkpoints': True,
    'checkpoint_keep_last': 3,
    'resume_supported': False,
    'shuffle': True,
    'shuffle_seed': 42,
    'interleave': True,
    'shuffle_memory_mb': 256,
    'interleave_fan_in': 32,
    'dataset_shard_mb': 512,
    'analyse_dataset': True,
    'dataset_expansion_factor': 8,
    'disk_reserve_gb': 10,
    'execution_image': '',
}
DATA_SUFFIXES = ('.pgn.tar', '.tar', '.tar.gz', '.tgz', '.tar.bz2', '.tar.zst', '.pgn', '.pgn.bz2', '.pgn.gz', '.pgn.zst', '.vf', '.viri', '.vf.bz2', '.vf.gz', '.vf.zst', '.viri.zst')


def vault():
    key = settings.TRAINING_CREDENTIAL_KEY
    if settings.TRAINING_CREDENTIAL_KEY_FILE:
        try:
            key = Path(settings.TRAINING_CREDENTIAL_KEY_FILE).read_bytes().strip()
        except OSError:
            raise ValidationError('The credential encryption key is unavailable. Contact the administrator.') from None
    if not key:
        raise ValidationError('Hugging Face connections need a server encryption key. Ask the administrator to configure MATTBENCH_CREDENTIAL_KEY_FILE.')
    try:
        return Fernet(key)
    except (ValueError, TypeError):
        raise ValidationError('The server credential encryption key is invalid.') from None


def save_credential(user, token):
    wrapper = vault()
    if not re.fullmatch(r'hf_[A-Za-z0-9]{10,250}', token):
        raise ValidationError('Enter a Hugging Face access token.')
    try:
        response = requests.get('https://huggingface.co/api/whoami-v2', headers={'Authorization': 'Bearer ' + token}, timeout=20)
        response.raise_for_status()
        account = response.json()['name']
    except (requests.RequestException, ValueError, KeyError):
        raise ValidationError('Hugging Face could not verify this token. Check its permissions and try again.') from None
    key = Fernet.generate_key()
    HuggingFaceCredential.objects.update_or_create(user=user, defaults={
        'account': account,
        'wrapped_key': wrapper.encrypt(key).decode(),
        'ciphertext': Fernet(key).encrypt(token.encode()).decode(),
    })


def hf_token(user_id):
    credential = HuggingFaceCredential.objects.filter(user_id=user_id).first()
    if not credential:
        raise ValidationError('Connect Hugging Face in your profile first.')
    try:
        key = vault().decrypt(credential.wrapped_key.encode())
        return Fernet(key).decrypt(credential.ciphertext.encode()).decode()
    except (InvalidToken, ValueError):
        raise ValidationError('Hugging Face credentials could not be decrypted. Reconnect the account or restore the server key.') from None


def repo_id(value):
    value = value.strip().rstrip('/')
    if value.startswith('https://huggingface.co/datasets/'):
        value = value.removeprefix('https://huggingface.co/datasets/')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,95}/[A-Za-z0-9][A-Za-z0-9_.-]{0,95}', value) or '..' in value:
        raise ValidationError('Use a Hugging Face dataset URL or namespace/dataset.')
    return value


def relative_path(value):
    if not isinstance(value, str) or not value or len(value) > 512 or '\\' in value or ':' in value:
        raise ValidationError('Use a relative file path with forward slashes.')
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ('..', '.', '.git') for part in value.split('/')) or any(ord(c) < 32 for c in value):
        raise ValidationError('File paths must stay inside the training directory.')
    return value


def validate_schedule(files, config):
    if not isinstance(files, dict) or not 1 <= len(files) <= 32:
        raise ValidationError('Upload between 1 and 32 schedule files.')
    if len(json.dumps(files).encode()) > 1024 * 1024:
        raise ValidationError('Schedule files must total less than 1 MB.')
    for name, source in files.items():
        relative_path(name)
        if not isinstance(source, str) or '\x00' in source:
            raise ValidationError('Schedules must contain text files.')
        if name != 'Cargo.lock' and not name.endswith(('.rs', '.toml', '.json', '.py', '.sh', '.txt')):
            raise ValidationError('Supported schedule files: Rust, TOML, JSON, Python, shell and text.')
    if not any(name.endswith('.rs') and source.strip() for name, source in files.items()):
        raise ValidationError('Add your Rust training schedule before saving.')
    if not isinstance(config, dict) or set(config) - set(DEFAULT_SETTINGS):
        raise ValidationError('Unknown schedule settings.')
    config = {**DEFAULT_SETTINGS, **config}
    if not isinstance(config['example_name'], str) or config['example_name'] and not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', config['example_name']):
        raise ValidationError('Use a simple Cargo example name, or leave it empty for a custom build.')
    if config['example_name']:
        relative_path(config['example_manifest'])
        relative_path(config['example_source'])
        if config['example_source'] not in files or not files[config['example_source']].strip():
            raise ValidationError('Add the schedule at %s, or change example_source to its file path.' % config['example_source'])
    if not re.fullmatch(r'https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', config['bullet_repo']):
        raise ValidationError('Use an HTTPS GitHub repository URL without credentials.')
    if not isinstance(config['bullet_ref'], str) or not re.fullmatch(r'[A-Za-z0-9_./-]{1,200}', config['bullet_ref']) or config['bullet_ref'].startswith('-'):
        raise ValidationError('Enter a Bullet branch, tag or commit.')
    if config['backend'] not in ('cuda', 'rocm'):
        raise ValidationError('Choose CUDA or ROCm.')
    for name in ('build', 'run'):
        command = config[name]
        if not isinstance(command, list) or not 1 <= len(command) <= 64 or any(not isinstance(arg, str) or not arg or len(arg) > 1024 or '\x00' in arg for arg in command):
            raise ValidationError('%s must be a JSON array of command arguments.' % name.capitalize())
    for name, minimum, maximum in (
        ('threads', 1, 512), ('min_vram_gb', 1, 2048), ('min_disk_gb', 1, 1000000),
        ('max_hours', 1, 8760), ('network_min_bytes', 1, 8 * 1024 ** 3), ('network_max_bytes', 1, 8 * 1024 ** 3),
        ('checkpoint_keep_last', 0, 10000),
        ('shuffle_memory_mb', 16, 65536), ('interleave_fan_in', 2, 256),
        ('dataset_shard_mb', 4, 16384),
        ('dataset_expansion_factor', 1, 1000), ('disk_reserve_gb', 1, 10000),
    ):
        if type(config[name]) is not int or not minimum <= config[name] <= maximum:
            raise ValidationError('%s must be between %d and %d.' % (name.replace('_', ' '), minimum, maximum))
    if config['network_min_bytes'] > config['network_max_bytes']:
        raise ValidationError('Minimum network size exceeds maximum size.')
    relative_path(config['network_glob'])
    if type(config['skip_broken_games']) is not bool:
        raise ValidationError('Skip broken games must be true or false.')
    for field in ('delete_uploaded_checkpoints', 'resume_supported', 'shuffle', 'interleave', 'analyse_dataset'):
        if type(config[field]) is not bool:
            raise ValidationError('%s must be true or false.' % field)
    if type(config['shuffle_seed']) is not int or not 0 <= config['shuffle_seed'] < 2 ** 63:
        raise ValidationError('Shuffle seed must be a nonnegative 63-bit integer.')
    fill = str(config['fill_missing_evals'])
    if fill not in ('', 'prev', 'next') and not (re.fullmatch(r'-?\d{1,5}', fill) and -32768 <= int(fill) <= 32767):
        raise ValidationError('Missing evaluations: leave empty, use prev/next, or an integer from -32768 to 32767.')
    config['fill_missing_evals'] = fill
    if not isinstance(config['execution_image'], str) or config['execution_image'] and not re.fullmatch(r'[^\s]+@sha256:[a-f0-9]{64}', config['execution_image']):
        raise ValidationError('Execution images must be pinned by SHA256 digest.')
    return config


def resolve_inputs(run):
    from huggingface_hub import HfApi
    config = run.snapshot['settings']
    api = HfApi(token=hf_token(run.owner_id))
    sources = run.dataset.get('stages') or [run.dataset]
    stages = []
    selected = []
    resolved = {}
    for index, source in enumerate(sources):
        key = (source['repo'], source['ref'], tuple(source['patterns']))
        if key in resolved:
            stages.append({**source, **resolved[key]})
            continue
        info = api.dataset_info(source['repo'], revision=source['ref'], files_metadata=True)
        if not re.fullmatch(r'[0-9a-f]{40}', info.sha):
            raise ValidationError('Could not pin the dataset revision.')
        from OpenBench.dataset_manifest import resolve_dataset
        metadata = resolve_dataset(source['repo'], info, source['patterns'], hf_token(run.owner_id))
        files = [{**file, 'repo': source['repo'], 'commit': info.sha, 'stage': index} for file in metadata.pop('files')]
        resolved[key] = {**metadata, 'commit': info.sha, 'size': sum(file['size'] for file in files), 'file_indices': list(range(len(selected), len(selected) + len(files)))}
        selected.extend(files)
        stages.append({**source, **resolved[key]})
    if len(selected) > 4096:
        raise ValidationError('Select at most 4096 dataset files per run.')
    repository = config['bullet_repo'].removeprefix('https://github.com/')
    response = requests.get('https://api.github.com/repos/%s/commits/%s' % (repository, quote(config['bullet_ref'], safe='')), timeout=30)
    response.raise_for_status()
    commit = response.json()['sha']
    if not re.fullmatch(r'[0-9a-f]{40}', commit) or not re.fullmatch(r'[0-9a-f]{40}', info.sha):
        raise ValidationError('Could not pin the dataset and Bullet revisions.')
    snapshot = {**run.snapshot, 'bullet_commit': commit}
    dataset = {**run.dataset, **resolved[next(iter(resolved))], 'commit': stages[0]['commit'], 'files': selected, 'size': sum(file['size'] for file in selected)}
    dataset.pop('file_indices', None)
    if run.dataset.get('stages'):
        dataset['stages'] = stages
    return snapshot, dataset


def download_access(run, file_index, stage=None):
    from huggingface_hub import get_hf_file_metadata, hf_hub_url
    from OpenBench.training_datasets import dataset_input
    dataset = dataset_input(run, stage)
    file = dataset['files'][file_index]
    revision = file.get('commit', dataset['commit'])
    metadata = get_hf_file_metadata(hf_hub_url(file.get('repo', dataset['repo']), file['path'], repo_type='dataset', revision=revision), token=hf_token(run.owner_id))
    if metadata.size != file['size'] or metadata.commit_hash != revision:
        raise ValidationError('Dataset metadata changed unexpectedly.')
    if metadata.xet_file_data:
        return {'xet_hash': metadata.xet_file_data.file_hash, 'size': file['size']}
    location = urlsplit(metadata.location)
    if location.scheme != 'https' or not location.hostname:
        raise ValidationError('Invalid dataset download location.')
    if location.hostname == 'huggingface.co':
        return {'proxy': True, 'size': file['size']}
    return {'url': metadata.location, 'size': file['size']}


def xet_access(run, file_index=None, stage=None):
    from OpenBench.training_datasets import dataset_input
    dataset = dataset_input(run, stage)
    file = dataset['files'][file_index] if file_index is not None else {}
    url = 'https://huggingface.co/api/datasets/%s/xet-read-token/%s' % (file.get('repo', dataset['repo']), file.get('commit', dataset['commit']))
    response = requests.get(url, headers={'Authorization': 'Bearer ' + hf_token(run.owner_id)}, timeout=25)
    response.raise_for_status()
    result = response.json()
    return {name: result[name] for name in ('accessToken', 'exp', 'casUrl')}


def digest_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()
