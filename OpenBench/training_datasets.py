import base64
import json
import hashlib
import re
from urllib.parse import quote

import requests
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import JsonResponse
from django.views.decorators.http import require_POST

from OpenBench.dataset_manifest import MANIFEST_PATH, analysis_files, dataset_statistics, read_manifest, remaining_steps, validate_manifest
from OpenBench.lifecycle import record_event
from OpenBench.models import TrainingRun
from OpenBench.training import hf_token
from OpenBench.training_api import json_body, owned_run, worker_endpoint


def raw_source_input(run, stage=None):
    if stage is None:
        if run.dataset.get('stages'):
            raise ValidationError('Choose a dataset stage.')
        return run.dataset
    if not str(stage).isdigit() or not 0 <= int(stage) < len(run.dataset.get('stages', [])):
        raise ValidationError('Invalid dataset stage.')
    source = run.dataset['stages'][int(stage)]
    return {**source, 'files': [run.dataset['files'][index] for index in source['file_indices']]}


def source_key(source):
    return hashlib.sha256(json.dumps([source['repo'], source['commit'], [file['path'] for file in source['files']]]).encode()).hexdigest()


def source_input(run, stage=None):
    source = raw_source_input(run, stage)
    key = source_key(source)
    for saved in run.preparation.values():
        if isinstance(saved, dict) and saved.get('source_key') == key and saved.get('dataset'):
            return {**source, **{name: value for name, value in saved['dataset'].items() if name in ('repo', 'commit', 'files', 'size', 'manifest', 'manifest_present', 'statistics', 'analysis_report')}}
    return source


def dataset_input(run, stage=None):
    return source_input(run, stage)


def effective_dataset(run):
    if not run.dataset.get('stages'):
        return {key: value for key, value in source_input(run).items() if key != 'file_indices'}
    files = []
    stages = []
    seen = {}
    for index in range(len(run.dataset['stages'])):
        source = source_input(run, index)
        key = (source['repo'], source['commit'], tuple(file['path'] for file in source['files']))
        if key not in seen:
            seen[key] = list(range(len(files), len(files) + len(source['files'])))
            files.extend(source['files'])
        stages.append({**{key: value for key, value in source.items() if key != 'files'}, 'file_indices': seen[key]})
    return {**run.dataset, 'stages': stages, 'files': files, 'size': sum(file['size'] for file in files), 'commit': stages[0]['commit']}


def stage_key(stage):
    return str(stage) if stage is not None else 'default'


@worker_endpoint
@require_POST
def prepare(request, pk):
    run = owned_run(request, pk)
    if run.state not in ('DOWNLOADING', 'CONVERTING') or run.cancel_requested:
        raise ValidationError('Dataset preparation is not available at this stage.')
    stage = request.GET.get('stage')
    source = source_input(run, stage)
    steps = remaining_steps(source, run.snapshot['settings'])
    plan = {'mode': 'ready' if not steps else 'local', 'steps': steps, 'repo': source['repo'], 'statistics': source.get('statistics', {}), 'analysis_report': source.get('analysis_report', ''), 'prefix': 'data/run-%d-%s/' % (run.pk, stage_key(stage)), 'source_key': source_key(raw_source_input(run, stage))}
    if steps or not source.get('manifest_present', False) or source.get('statistics', {}).get('analysis') and not source.get('analysis_report'):
        from huggingface_hub import HfApi
        refs = HfApi(token=hf_token(run.owner_id)).list_repo_refs(source['repo'], repo_type='dataset')
        if source.get('ref', 'main') in [branch.name for branch in refs.branches]:
            plan.update(mode='prepare', branch=source.get('ref', 'main'))
    with transaction.atomic():
        locked = TrainingRun.objects.select_for_update().get(pk=pk)
        from OpenBench.training_workloads import check_claim
        check_claim(request, locked)
        previous = locked.preparation.get(stage_key(stage), {})
        if isinstance(previous, dict) and previous.get('published') and previous.get('dataset'):
            plan.update({key: previous[key] for key in ('published', 'dataset') if key in previous})
        locked.preparation = {**locked.preparation, stage_key(stage): plan}
        locked.save(update_fields=['preparation'])
    return JsonResponse(plan)


def publication_context(request, pk):
    run = owned_run(request, pk)
    stage = request.GET.get('stage')
    source = source_input(run, stage)
    plan = run.preparation.get(stage_key(stage), {})
    if run.state != 'CONVERTING' or run.cancel_requested or not isinstance(plan, dict) or plan.get('mode') != 'prepare':
        raise ValidationError('This run is not publishing dataset preparation.')
    return run, stage, source, plan


@worker_endpoint
def dataset_token(request, pk):
    run, stage, source, plan = publication_context(request, pk)
    response = requests.get('https://huggingface.co/api/datasets/%s/xet-write-token/%s' % (source['repo'], quote(plan['branch'], safe='')), headers={'Authorization': 'Bearer ' + hf_token(run.owner_id)}, timeout=30)
    response.raise_for_status()
    token = response.json()
    result = JsonResponse(token)
    result['X-Xet-Cas-Url'] = token['casUrl']
    result['X-Xet-Access-Token'] = token['accessToken']
    result['X-Xet-Token-Expiration'] = str(token['exp'])
    return result


@worker_endpoint
@require_POST
def publish_dataset(request, pk):
    run, stage, source, plan = publication_context(request, pk)
    if plan.get('published'):
        return JsonResponse({'revision': plan['published'], 'stored': True})
    data = json_body(request, limit=2 * 1024 * 1024)
    files = data.get('files')
    statistics = data.get('statistics', {})
    validate_manifest({'version': 1, 'files': files, 'statistics': statistics})
    if isinstance(statistics.get('analysis'), str):
        statistics['analysis'] = '\n'.join(line for line in statistics['analysis'].splitlines() if not line.lstrip().lower().startswith('progress:'))
    changed = any(step in plan['steps'] for step in ('convert', 'extract', 'shuffle', 'interleave'))
    original = {file['path']: file for file in source['files']}
    shuffled = 'shuffle' in plan['steps'] or all(file.get('shuffled', False) for file in source['files'])
    interleaved = 'shuffle' in plan['steps'] or 'interleave' in plan['steps'] or all(file.get('interleaved', False) for file in source['files'])
    for file in files:
        if set(file) - {'path', 'size', 'sha256', 'format', 'shuffled', 'interleaved'}:
            raise ValidationError('Invalid published file metadata.')
        expected_shuffle = shuffled if changed else original.get(file['path'], {}).get('shuffled', False)
        expected_interleave = interleaved if changed else original.get(file['path'], {}).get('interleaved', False)
        if file['format'] != 'vf' or file['shuffled'] != expected_shuffle or file.get('interleaved', False) != expected_interleave:
            raise ValidationError('Published file readiness does not match the preparation plan.')
        if changed:
            if not re.fullmatch(re.escape(plan['prefix']) + r'[0-9]{5}\.vf', file['path']) or not re.fullmatch(r'[a-f0-9]{64}', file.get('sha256', '')):
                raise ValidationError('New files must use the assigned paths and SHA256 checksums.')
        elif file['path'] not in original or any(file.get(key) != original[file['path']].get(key) for key in ('size', 'sha256')):
            raise ValidationError('Analysis must preserve the dataset files.')
    if not changed and set(original) != {file['path'] for file in files}:
        raise ValidationError('Analysis must preserve every selected file.')
    if any(step in plan['steps'] for step in ('shuffle', 'interleave')) and not any(step in plan['steps'] for step in ('convert', 'extract')) and sum(file['size'] for file in files) != source['size']:
        raise ValidationError('Game ordering must preserve dataset size.')
    if 'analyse' in plan['steps'] and not isinstance(statistics.get('analysis'), str):
        raise ValidationError('Dataset analysis is missing.')
    from huggingface_hub import HfApi
    token = hf_token(run.owner_id)
    api = HfApi(token=token)
    info = api.dataset_info(source['repo'], revision=plan['branch'], files_metadata=True)
    current = read_manifest(source['repo'], info, token)
    if current is None:
        if source.get('manifest_present'):
            raise ValidationError('dataset.json was removed during preparation.')
        from OpenBench.dataset_manifest import resolve_dataset
        current = resolve_dataset(source['repo'], info, ['*'], token)['manifest']
    previous = source.get('manifest', {'version': 1, 'files': source['files']})
    publication = {'run': run.pk, 'stage': stage_key(stage), 'payload': hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()}
    if current and current.get('publication') == publication:
        return finish_publication(run, stage, source, plan, current, files, statistics, info.sha)
    if current is not None:
        old_entries = {file['path']: file for file in previous['files']}
        current_entries = {file['path']: file for file in current['files']}
        if any(current_entries.get(path) != old_entries.get(path) for path in original):
            raise ValidationError('This dataset was prepared by another run. Refresh the dataset and start a new run.')
    siblings = {file.rfilename: file for file in info.siblings}
    for path, file in original.items():
        actual = siblings.get(path)
        if actual is None or actual.size != file['size'] or file.get('blob_id') and actual.blob_id != file['blob_id'] or file.get('sha256') and actual.lfs and actual.lfs.sha256 != file['sha256']:
            raise ValidationError('Source files changed during preparation.')
    catalogue = current['files']
    preparations = list(current.get('preparations', []))
    if changed:
        inputs = [{**{key: file[key] for key in ('path', 'size', 'sha256', 'format', 'shuffled')}, 'interleaved': file.get('interleaved', False)} for file in source['files']]
        preparations.append({'inputs': inputs, 'files': files, 'source_revision': source['commit'], 'run': run.pk, 'stage': stage_key(stage)})
    analyses = list(current.get('analyses', []))
    report = ''
    if statistics.get('analysis'):
        report = 'analysis/run-%d-%s.txt' % (run.pk, stage_key(stage))
        analyses = [item for item in analyses if item['report'] != report]
        analyses.append({'files': analysis_files(files), 'statistics': statistics, 'report': report, 'source_revision': source['commit'], 'run': run.pk, 'stage': stage_key(stage)})
    manifest = validate_manifest({**current, 'version': 1, 'files': catalogue, 'preparations': preparations, 'statistics': statistics if analysis_files(files) == analysis_files(catalogue) else current.get('statistics', {}), 'analyses': analyses, 'publication': publication})
    lines = [{'key': 'header', 'value': {'summary': 'Update dataset preparation for run %d' % run.pk, 'description': '', 'parentCommit': info.sha}}]
    if changed:
        lines.extend({'key': 'lfsFile', 'value': {'path': file['path'], 'algo': 'sha256', 'oid': file['sha256'], 'size': file['size']}} for file in files)
    if report:
        text = 'Dataset analysis\nRepository: %s\nSource revision: %s\nRun: %d / stage: %s\n\nFiles analysed:\n%s\n\nStatistics:\n%s\n\nAnalysis output:\n%s\n' % (
            source['repo'], source['commit'], run.pk, stage_key(stage),
            '\n'.join('%s (%d bytes)' % (file['path'], file['size']) for file in files),
            json.dumps({key: value for key, value in statistics.items() if key != 'analysis'}, indent=2, sort_keys=True), statistics['analysis'])
        for path in (report, 'analysis.txt'):
            lines.append({'key': 'file', 'value': {'path': path, 'encoding': 'base64', 'content': base64.b64encode(text.encode()).decode()}})
    lines.append({'key': 'file', 'value': {'path': MANIFEST_PATH, 'encoding': 'base64', 'content': base64.b64encode((json.dumps(manifest, indent=2) + '\n').encode()).decode()}})
    response = requests.post('https://huggingface.co/api/datasets/%s/commit/%s' % (source['repo'], quote(plan['branch'], safe='')), headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/x-ndjson'}, data='\n'.join(json.dumps(line) for line in lines).encode(), timeout=120)
    response.raise_for_status()
    revision = response.json()['commitOid']
    return finish_publication(run, stage, source, plan, manifest, files, statistics, revision)


def finish_publication(run, stage, source, plan, manifest, files, statistics, revision):
    _, report = dataset_statistics(manifest, files)
    dataset = {**source, 'analysis_report': report, 'commit': revision, 'manifest': manifest, 'manifest_present': True, 'statistics': statistics, 'files': files, 'size': sum(file['size'] for file in files)}
    with transaction.atomic():
        locked = TrainingRun.objects.select_for_update().get(pk=run.pk)
        if run.workload_size and (locked.claim_id != run.claim_id or locked.worker_id != run.worker_id or locked.state != 'CONVERTING'):
            raise ValidationError('The dataset preparation claim has expired.')
        locked.preparation = {**locked.preparation, stage_key(stage): {**plan, 'published': revision, 'analysis_report': report, 'statistics': statistics, 'dataset': dataset}}
        locked.save(update_fields=['preparation'])
        record_event('dataset.updated', locked, run.owner_id, {'repo': source['repo'], 'revision': revision, 'stage': stage}, key='dataset:%d:%s:%s' % (run.pk, stage_key(stage), revision))
    return JsonResponse({'revision': revision, 'stored': True})
