import fnmatch
import json
import re

from django.core.exceptions import ValidationError

from OpenBench.training import DATA_SUFFIXES, relative_path


MANIFEST_PATH = 'dataset.json'


def analysis_files(files):
    return sorted([{'path': file['path'], 'size': file['size'], 'sha256': file.get('sha256') or ''} for file in files], key=lambda file: file['path'])


def dataset_statistics(manifest, files):
    selected = analysis_files(files)
    for analysis in reversed(manifest.get('analyses', [])):
        if analysis_files(analysis['files']) == selected:
            return analysis['statistics'], analysis['report']
    return (manifest.get('statistics', {}), '') if {file['path'] for file in files} == {file['path'] for file in manifest['files']} else ({}, '')


def validate_manifest(manifest):
    if not isinstance(manifest, dict) or type(manifest.get('version')) is not int or manifest['version'] != 1:
        raise ValidationError('dataset.json must use version 1.')
    files = manifest.get('files')
    if not isinstance(files, list) or not 1 <= len(files) <= 4096:
        raise ValidationError('dataset.json must list between 1 and 4096 files.')
    seen = set()
    for file in files:
        if not isinstance(file, dict) or not isinstance(file.get('path'), str):
            raise ValidationError('Invalid dataset file.')
        relative_path(file['path'])
        if file['path'] in seen or not file['path'].lower().endswith(DATA_SUFFIXES):
            raise ValidationError('Dataset paths must be unique supported data files.')
        seen.add(file['path'])
        if type(file.get('size')) is not int or file['size'] <= 0:
            raise ValidationError('Dataset files require a positive size.')
        if file.get('format') not in ('pgn', 'vf') or type(file.get('shuffled')) is not bool:
            raise ValidationError('Each file requires format (pgn or vf) and shuffled (true or false).')
        if type(file.get('interleaved', False)) is not bool or file.get('interleaved', False) and file['format'] != 'vf':
            raise ValidationError('Only Viriformat files can be marked interleaved, using true or false.')
        if file['shuffled'] and file['format'] != 'vf':
            raise ValidationError('Only Viriformat files can be marked shuffled.')
        if file['path'].lower().endswith(('.pgn', '.pgn.gz', '.pgn.bz2', '.pgn.zst')) and file['format'] != 'pgn':
            raise ValidationError('PGN paths must declare format pgn.')
        if file['path'].lower().endswith(('.vf', '.viri', '.vf.gz', '.vf.bz2', '.vf.zst', '.viri.zst')) and file['format'] != 'vf':
            raise ValidationError('Viriformat paths must declare format vf.')
        if file.get('sha256') and (not isinstance(file['sha256'], str) or not re.fullmatch(r'[a-f0-9]{64}', file['sha256'])):
            raise ValidationError('Invalid dataset SHA256.')
        sources = file.get('sources', [])
        if not isinstance(sources, list) or len(sources) > 4096 or any(not isinstance(path, str) for path in sources):
            raise ValidationError('Invalid source paths.')
        for path in sources:
            relative_path(path)
    if not isinstance(manifest.get('statistics', {}), dict) or len(json.dumps(manifest)) > 2 * 1024 ** 2:
        raise ValidationError('Invalid or oversized dataset metadata.')
    if 'analysis' in manifest.get('statistics', {}) and not isinstance(manifest['statistics']['analysis'], str):
        raise ValidationError('Dataset analysis must be text.')
    preparations = manifest.get('preparations', [])
    if not isinstance(preparations, list) or len(preparations) > 4096:
        raise ValidationError('Invalid dataset preparations.')
    for preparation in preparations:
        if not isinstance(preparation, dict):
            raise ValidationError('Invalid dataset preparation.')
        for key in ('inputs', 'files'):
            validate_manifest({'version': 1, 'files': preparation.get(key)})
        if analysis_files(preparation['inputs']) == analysis_files(preparation['files']):
            raise ValidationError('A dataset preparation must produce different files.')
    analyses = manifest.get('analyses', [])
    if not isinstance(analyses, list) or len(analyses) > 4096:
        raise ValidationError('Invalid dataset analyses.')
    for analysis in analyses:
        if not isinstance(analysis, dict) or not isinstance(analysis.get('statistics'), dict) or not isinstance(analysis['statistics'].get('analysis'), str):
            raise ValidationError('Dataset analyses require statistics and analysis text.')
        relative_path(analysis.get('report'))
        if not analysis['report'].endswith('.txt'):
            raise ValidationError('Dataset analysis reports must be text files.')
        files = analysis.get('files')
        if not isinstance(files, list) or not files or len(files) > 4096:
            raise ValidationError('Dataset analyses must identify their input files.')
        for file in files:
            if not isinstance(file, dict) or type(file.get('size')) is not int or file['size'] <= 0:
                raise ValidationError('Invalid analysis input file.')
            relative_path(file.get('path'))
            if file.get('sha256') and (not isinstance(file['sha256'], str) or not re.fullmatch(r'[a-f0-9]{64}', file['sha256'])):
                raise ValidationError('Invalid analysis input checksum.')
    return manifest


def read_manifest(repo, info, token):
    from huggingface_hub import hf_hub_download
    entry = next((file for file in info.siblings if file.rfilename == MANIFEST_PATH), None)
    if entry is None:
        return None
    if not entry.size or entry.size > 2 * 1024 ** 2:
        raise ValidationError('dataset.json exceeds the supported size.')
    path = hf_hub_download(repo, MANIFEST_PATH, repo_type='dataset', revision=info.sha, token=token)
    try:
        with open(path, encoding='utf-8') as stream:
            return validate_manifest(json.load(stream))
    except (ValueError, UnicodeError) as error:
        raise ValidationError('Invalid dataset.json: %s' % error) from None


def inferred_file(file):
    name = file.rfilename.lower()
    return {'path': file.rfilename, 'size': file.size, 'format': 'vf' if any(name.endswith(suffix) for suffix in ('.vf', '.viri', '.vf.gz', '.vf.bz2', '.vf.zst', '.viri.zst')) else 'pgn', 'shuffled': False}


def verified_files(entries, siblings):
    files = []
    for entry in entries:
        relative_path(entry['path'])
        actual = siblings.get(entry['path'])
        if actual is None or type(actual.size) is not int or actual.size <= 0 or actual.size != entry['size']:
            raise ValidationError('Dataset file is missing or its size differs from dataset.json: ' + entry['path'])
        sha256 = actual.lfs.sha256 if actual.lfs else ''
        if entry.get('sha256') and sha256 and entry['sha256'] != sha256:
            raise ValidationError('Dataset checksum differs from dataset.json: ' + entry['path'])
        files.append({**entry, 'sha256': sha256 or entry.get('sha256', ''), 'blob_id': actual.blob_id})
    return files


def resolve_dataset(repo, info, patterns, token):
    manifest = read_manifest(repo, info, token)
    siblings = {file.rfilename: file for file in info.siblings}
    entries = manifest['files'] if manifest else [inferred_file(file) for file in info.siblings if file.rfilename.lower().endswith(DATA_SUFFIXES)]
    selected = {}
    matched_patterns = set()
    for pattern in patterns:
        for entry in entries:
            if fnmatch.fnmatchcase(entry['path'], pattern):
                selected[entry['path']] = entry
                matched_patterns.add(pattern)
    for entry in entries:
        sources = entry.get('sources', [])
        if sources and all(any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns) for path in sources):
            selected[entry['path']] = entry
            matched_patterns.update(pattern for pattern in patterns if any(fnmatch.fnmatchcase(path, pattern) for path in sources))
        elif entry['path'] not in selected:
            for path in sources:
                matches = [pattern for pattern in patterns if fnmatch.fnmatchcase(path, pattern)]
                if matches:
                    if path not in siblings:
                        raise ValidationError('An original file required for this subset is missing: ' + path)
                    selected[path] = inferred_file(siblings[path])
                    matched_patterns.update(matches)
    for pattern in patterns:
        if pattern in matched_patterns:
            continue
        for preparation in (manifest or {}).get('preparations', []):
            for entry in preparation['files']:
                if fnmatch.fnmatchcase(entry['path'], pattern):
                    selected[entry['path']] = entry
    files = verified_files(list(selected.values()), siblings)
    if not files or len(files) > 4096:
        raise ValidationError('Select between 1 and 4096 dataset files.')
    seen = set()
    preparations = (manifest or {}).get('preparations', [])
    while True:
        key = json.dumps(analysis_files(files), sort_keys=True)
        if key in seen:
            raise ValidationError('Dataset preparation references form a cycle.')
        seen.add(key)
        prepared = next((item for item in reversed(preparations) if analysis_files(item['inputs']) == analysis_files(files)), None)
        if prepared is None:
            break
        files = verified_files(prepared['files'], siblings)
    statistics, report = dataset_statistics(manifest, files) if manifest else ({}, '')
    return {'files': files, 'analysis_report': report, 'manifest_present': manifest is not None, 'manifest': manifest or {'version': 1, 'files': entries}, 'statistics': statistics}


def remaining_steps(dataset, config):
    files = dataset['files']
    steps = []
    if any(file.get('format') != 'vf' for file in files):
        steps.append('convert')
    if any(not file['path'].lower().endswith(('.vf', '.viri', '.pgn')) for file in files):
        steps.append('extract')
    if config.get('shuffle', True) and not all(file.get('shuffled', False) for file in files):
        steps.append('shuffle')
    if config.get('interleave', True) and not all(file.get('interleaved', False) for file in files):
        steps.append('interleave')
    if config.get('analyse_dataset', True) and (not dataset.get('statistics', {}).get('analysis') or dataset.get('statistics', {}).get('analysis_kind') == 'pgn_headers'):
        steps.append('analyse')
    return steps


def required_disk_bytes(dataset, config):
    total = 0
    seen = set()
    for stage in dataset.get('stages') or [dataset]:
        indices = stage.get('file_indices', list(range(len(dataset['files']))))
        key = tuple(indices)
        if key in seen:
            continue
        seen.add(key)
        source = {**stage, 'files': [dataset['files'][index] for index in indices]}
        steps = remaining_steps(source, config)
        size = sum(file['size'] for file in source['files'])
        factor = config.get('dataset_expansion_factor', 8) * 4 if 'convert' in steps or 'extract' in steps else 4 if 'shuffle' in steps or 'interleave' in steps else 1
        total += size * factor
    return total
