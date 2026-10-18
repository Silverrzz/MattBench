import copy
import hashlib
import json
import math
import re
from functools import lru_cache
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files import File
from django.core.files.storage import FileSystemStorage
from django.db import transaction
from django.utils import timezone

from OpenBench.models import DatasetUpload, Engine, EngineConfig, Network, Profile, Test, TrainingArtifact, TrainingRun, TrainingSchedule, Workflow, WorkflowRun, WorkflowStep
from OpenBench.worker_policy import validate_policy


KINDS = ('datagen', 'upload', 'train', 'test', 'decision', 'approval')
OUTCOMES = ('completed', 'pass', 'fail', 'inconclusive', 'error', 'cancelled')
TERMINAL = ('COMPLETED', 'SKIPPED', 'CANCELLED')
TEST_FIELDS = ('book_name', 'dev_repo', 'dev_engine', 'dev_options', 'dev_network', 'dev_netname', 'dev_time_control',
               'base_repo', 'base_engine', 'base_options', 'base_network', 'base_netname', 'base_time_control',
               'workload_size', 'priority', 'throughput', 'scale_method', 'scale_nps', 'syzygy_wdl', 'syzygy_adj',
               'win_adj', 'draw_adj', 'test_mode', 'elolower', 'eloupper', 'alpha', 'beta', 'lowerllr', 'upperllr',
               'max_games', 'genfens_args', 'play_reverses', 'use_tri', 'use_penta')


def validate_definition(value):
    if not isinstance(value, dict) or set(value) - {'profile', 'engine', 'steps', 'edges'}:
        raise ValidationError('Invalid workflow definition.')
    from OpenBench.training_profiles import resolve_profile
    resolve_profile(value.get('profile', 'standard'))
    if not EngineConfig.objects.filter(pk=value.get('engine'), enabled=True).exists():
        raise ValidationError('Choose an enabled engine.')
    steps, edges = value.get('steps'), value.get('edges', [])
    if not isinstance(steps, list) or not 1 <= len(steps) <= 64 or not isinstance(edges, list) or len(edges) > 256:
        raise ValidationError('Use 1–64 steps and at most 256 connections.')
    normalized = []
    keys = set()
    for step in steps:
        if not isinstance(step, dict) or set(step) - {'id', 'name', 'kind', 'config', 'inputs', 'worker', 'join', 'retries', 'timeout_hours'}:
            raise ValidationError('Invalid workflow step.')
        key = step.get('id', '')
        if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,63}', key) or key in keys:
            raise ValidationError('Give each step a unique identifier.')
        keys.add(key)
        if step.get('kind') not in KINDS or step.get('join', 'all') not in ('all', 'any'):
            raise ValidationError('Choose a supported step type and join rule.')
        if not isinstance(step.get('name'), str) or not 1 <= len(step['name'].strip()) <= 128:
            raise ValidationError('Give each step a name of at most 128 characters.')
        if not isinstance(step.get('config', {}), dict) or not isinstance(step.get('inputs', {}), dict):
            raise ValidationError('Invalid step configuration or inputs.')
        retries, timeout = step.get('retries', 0), step.get('timeout_hours', 168)
        if type(retries) is not int or not 0 <= retries <= 10 or type(timeout) is not int or not 1 <= timeout <= 8760:
            raise ValidationError('Use 0–10 retries and a deadline of 1–8760 hours.')
        normalized.append({**step, 'config': step.get('config', {}), 'inputs': step.get('inputs', {}), 'worker': validate_policy(step.get('worker', {})), 'join': step.get('join', 'all'), 'retries': retries, 'timeout_hours': timeout})
    graph = {key: [] for key in keys}
    connections = []
    for edge in edges:
        if not isinstance(edge, dict) or set(edge) != {'from', 'to', 'on'} or edge['from'] not in keys or edge['to'] not in keys or edge['from'] == edge['to']:
            raise ValidationError('Connect existing, different steps.')
        if not isinstance(edge['on'], list) or not edge['on'] or set(edge['on']) - set(OUTCOMES):
            raise ValidationError('Choose valid connection outcomes.')
        if any(e['from'] == edge['from'] and e['to'] == edge['to'] for e in connections):
            raise ValidationError('Connect each pair of steps once.')
        graph[edge['from']].append(edge['to'])
        connections.append(edge)
    visiting, visited = set(), set()
    def visit(key):
        if key in visiting:
            raise ValidationError('Workflow connections must not contain cycles. Use bounded error retries or another explicit training step.')
        if key in visited:
            return
        visiting.add(key)
        for child in graph[key]:
            visit(child)
        visiting.remove(key)
        visited.add(key)
    for key in keys:
        visit(key)
    by_key = {step['id']: step for step in normalized}
    @lru_cache(maxsize=64)
    def ancestors(key):
        parents = {edge['from'] for edge in connections if edge['to'] == key}
        return parents | set().union(*(ancestors(parent) for parent in parents)) if parents else set()
    for step in normalized:
        parents = ancestors(step['id'])
        for name, source in step['inputs'].items():
            if name not in ('dataset', 'network', 'source') or source not in parents:
                raise ValidationError('Step inputs must come from connected upstream steps.')
        required = {'upload': ('dataset', ('datagen',)), 'train': ('dataset', ('upload',)), 'test': ('network', ('train', 'test')), 'decision': ('source', KINDS)}.get(step['kind'])
        if required:
            name, kinds = required
            if step['inputs'].get(name) not in by_key or by_key[step['inputs'][name]]['kind'] not in kinds:
                raise ValidationError(step['name'] + ' needs a compatible upstream ' + name + '.')
    return {'profile': value.get('profile', 'standard'), 'engine': str(value['engine']), 'steps': normalized, 'edges': connections}


def workload_snapshot(test):
    values = {name: copy.deepcopy(getattr(test, name)) for name in TEST_FIELDS}
    values['execution'] = {key: copy.deepcopy(value) for key, value in test.execution.items() if key not in ('demo', 'workflow', 'worker_policy', 'progress', 'pending_progress', 'llr_allocation')}
    for side in ('dev', 'base'):
        values[side] = {name: getattr(getattr(test, side), name) for name in ('name', 'source', 'sha', 'bench')}
    return values


@transaction.atomic
def start(workflow, user, identifier=None):
    if workflow.owner_id != user.pk:
        raise ValidationError('Only the owner can run this workflow.')
    definition = validate_definition(workflow.definition)
    from OpenBench.training_profiles import resolve_profile
    definition['training_profile'] = resolve_profile(definition['profile'])
    from OpenBench.training_views import schedules_for
    for node in definition['steps']:
        config = node['config']
        if node['kind'] in ('datagen', 'test'):
            source = Test.objects.select_related('dev', 'base').filter(pk=config.get('template'), deleted=False).first()
            expected = 'DATAGEN' if node['kind'] == 'datagen' else 'SPRT'
            if source is None or source.test_mode != expected or source.dev_engine != EngineConfig.objects.get(pk=definition['engine']).name:
                raise ValidationError(node['name'] + ': choose a matching ' + expected + ' workload template.')
            if source.execution.get('demo'):
                raise ValidationError('Demo workloads cannot be workflow templates.')
            variant = source.execution.get('fastchess_variant', 'standard')
            if variant != definition['training_profile']['variant']:
                raise ValidationError('The workload variant must match the training profile.')
            config['workload'] = workload_snapshot(source)
            if node['kind'] == 'datagen':
                games = config.get('games', 10000000)
                if type(games) is not int or games < 2 or games > 10000000000 or games % 2:
                    raise ValidationError('Datagen targets must be an even number from 2 to 10 billion.')
            else:
                if config.get('control', 'template') not in ('template', 'nodes', 'stc', 'ltc'):
                    raise ValidationError('Choose fixed nodes, STC, LTC or the template time control.')
                if config.get('control') == 'nodes' and (type(config.get('nodes')) is not int or not 1 <= config['nodes'] <= 1000000000000):
                    raise ValidationError('Choose a positive node limit.')
                if config.get('max_games', 0) and (type(config['max_games']) is not int or not 2 <= config['max_games'] <= 10000000000):
                    raise ValidationError('Choose a valid SPRT game cap.')
        elif node['kind'] == 'train':
            schedule = schedules_for(user).filter(pk=config.get('schedule')).first()
            if schedule is None or 'mattbench-demo.json' in schedule.files:
                raise ValidationError('Choose an available runnable training schedule.')
            if schedule.engine_id and str(schedule.engine_id) != definition['engine']:
                raise ValidationError('The training schedule must match the workflow engine.')
            from OpenBench.schedule_builder import builder_state, generate_schedule
            spec, managed = builder_state(schedule)
            if not managed or spec['training_profile'] != definition['profile']:
                raise ValidationError('Choose a builder schedule for this workflow’s training profile.')
            _, files, options = generate_schedule(spec, definition['training_profile']['bullet_ref'], definition['training_profile'])
            config['schedule_snapshot'] = {'name': schedule.name, 'version': schedule.version, 'files': files, 'settings': options, 'environment': {}}
        elif node['kind'] == 'upload':
            from OpenBench.training import hf_token, repo_id
            hf_token(user.pk)
            config['repo'] = repo_id(config.get('repo', ''))
            if type(config.get('private', True)) is not bool:
                raise ValidationError('Choose the upload visibility.')
        elif node['kind'] == 'decision':
            if config.get('field') not in ('games', 'llr', 'loss', 'positions_per_second', 'superbatch') or config.get('operator') not in ('gt', 'gte', 'lt', 'lte', 'eq') or type(config.get('value')) not in (int, float) or not math.isfinite(config['value']):
                raise ValidationError('Choose a supported metric, comparison and numeric value.')
    run = WorkflowRun.objects.create(**({"id": identifier} if identifier else {}), workflow=workflow, owner=user, version=workflow.version, snapshot=definition)
    WorkflowStep.objects.bulk_create([WorkflowStep(run=run, key=node['id']) for node in definition['steps']])
    return run


def reference(step):
    return {'run': str(step.run_id), 'step': step.key}


def imported_network(artifact, step):
    from OpenBench.training_storage import storage_lock, verify_artifact
    with storage_lock():
        artifact = TrainingArtifact.objects.select_for_update().select_related('run__engine').get(pk=artifact.pk)
        verify_artifact(artifact)
        manifest = json.loads(artifact.run.snapshot.get('files', {}).get('mattbench-builder.json', '{}'))
        expected = manifest.get('export', {}).get('network_bytes')
        if expected and artifact.size != expected:
            raise ValidationError('The exported network size does not match the captured training profile.')
        if artifact.network_id:
            return artifact.network
        sha = artifact.sha256[:8].upper()
        storage = FileSystemStorage()
        existing = Network.objects.filter(sha256=sha).first()
        if existing:
            if existing.engine != artifact.run.engine.name or not storage.exists(sha):
                raise ValidationError('Network hash collision.')
            with storage.open(sha, 'rb') as source:
                if hashlib.file_digest(source, 'sha256').hexdigest() != artifact.sha256:
                    raise ValidationError('Network hash collision.')
            network = existing
        else:
            if storage.exists(sha):
                raise ValidationError('The network destination already exists.')
            with (Path(settings.TRAINING_ROOT) / artifact.path).open('rb') as source:
                destination = storage.save(sha, File(source))
            if destination != sha:
                storage.delete(destination)
                raise ValidationError('Network registration collided with another upload.')
            network = Network.objects.create(sha256=sha, name=('wf-%s-%s' % (str(step.run_id)[:8], step.key))[:64], engine=artifact.run.engine.name, author=step.run.owner.username)
        artifact.network = network
        artifact.save(update_fields=['network'])
        return network


def launch(step, node, outputs):
    config, run = node['config'], step.run
    if node['kind'] in ('datagen', 'test'):
        values = copy.deepcopy(config['workload'])
        for side in ('dev', 'base'):
            values[side] = Engine.objects.create(**values[side])
        values.update(author=run.owner.username, info='Workflow %s · %s' % (run.workflow.name, node['name']),
                      execution={**values['execution'], 'workflow': reference(step), 'worker_policy': node['worker']})
        approval = Profile.objects.get(user=run.owner)
        from OpenBench.config import OPENBENCH_CONFIG
        values['approved'] = approval.approver and not OPENBENCH_CONFIG['use_cross_approval']
        if node['kind'] == 'datagen':
            values.update(max_games=config.get('games', 10000000), upload_pgns='COMPACT')
        else:
            candidate = outputs[node['inputs']['network']]
            values.update(dev_network=candidate['network'], dev_netname=candidate['network_name'])
            values['dev'].bench = 0
            values['dev'].save(update_fields=['bench'])
            control = config.get('control', 'template')
            if control != 'template':
                tc = 'N=%d' % config['nodes'] if control == 'nodes' else config.get('time_control') or ('10.0+0.1' if control == 'stc' else '60.0+0.6')
                from OpenBench.utils import TimeControl
                tc = TimeControl.parse(tc)
                values.update(dev_time_control=tc, base_time_control=tc)
            values['execution']['workflow_game_cap'] = config.get('max_games', 0)
        step.test = Test.objects.create(**values)
        from OpenBench.lifecycle import test_event
        test_event('created', step.test, run.owner_id)
        if values['approved']:
            test_event('approved', step.test, run.owner_id)
    elif node['kind'] == 'upload':
        workload = Test.objects.get(pk=outputs[node['inputs']['dataset']]['test'])
        archive = Path(settings.MEDIA_ROOT) / 'PGNs' / ('%d.pgn.tar' % workload.pk)
        from OpenBench.utils import getRecentMachines
        if not archive.is_file() or getRecentMachines().filter(workload=workload.pk).exists() or timezone.now().timestamp() - archive.stat().st_mtime < 30:
            step.message = 'Waiting for final workers and PGN archive assembly.'
            return False
        step.upload = DatasetUpload.objects.create(owner=run.owner, workload=workload, repo=config['repo'], private=config.get('private', True), filename='workflow-%s-%s-%d.pgn.tar' % (str(run.pk)[:8], step.key, step.attempts + 1))
    elif node['kind'] == 'train':
        dataset = outputs[node['inputs']['dataset']]['dataset']
        step.training = TrainingRun.objects.create(owner=run.owner, engine_id=run.snapshot['engine'], name='wf-%s-%s' % (str(run.pk)[:8], step.key),
            snapshot={**config['schedule_snapshot'], 'workflow': reference(step), 'worker_policy': node['worker']},
            dataset=dataset, priority=config.get('priority', 0))
    elif node['kind'] == 'approval':
        step.state = 'WAITING'
        step.message = 'Waiting for the workflow owner’s decision.'
    elif node['kind'] == 'decision':
        import operator
        source = outputs[node['inputs']['source']]
        metric = source.get(config['field'], source.get('metrics', {}).get(config['field']))
        if type(metric) not in (int, float):
            raise ValidationError('The selected metric is unavailable.')
        result = getattr(operator, {'gt': 'gt', 'gte': 'ge', 'lt': 'lt', 'lte': 'le', 'eq': 'eq'}[config['operator']])(metric, config['value'])
        step.outputs = source
        finish(step, 'pass' if result else 'fail')
    if step.state == 'PENDING':
        step.state = 'RUNNING'
    step.attempts += 1
    step.started = timezone.now()
    step.message = '' if step.state == 'RUNNING' else step.message
    return True


def finish(step, outcome, message=''):
    step.state, step.outcome = 'COMPLETED', outcome
    step.finished = timezone.now()
    step.message = message


def poll(step, node):
    if step.test_id:
        test = Test.objects.get(pk=step.test_id)
        cap = node['config'].get('max_games', 0) if node['kind'] == 'test' else 0
        if cap and test.games >= cap and not test.finished:
            Test.objects.filter(pk=test.pk).update(finished=True)
            test.finished = True
        step.message = 'Waiting for workload approval.' if not test.approved else '%s games' % test.games
        if test.deleted or test.finished:
            step.outputs = {'test': test.pk, 'games': test.games, 'llr': test.currentllr}
            if node['kind'] == 'test':
                step.outputs.update(network=test.dev_network, network_name=test.dev_netname)
            finish(step, 'cancelled' if test.deleted else ('completed' if node['kind'] == 'datagen' and test.games >= test.max_games else 'pass' if test.passed else 'fail' if test.failed else 'inconclusive'))
    elif step.upload_id:
        task = DatasetUpload.objects.get(pk=step.upload_id)
        step.message = task.stage
        if task.state == 'COMPLETED':
            step.outputs = {'dataset': {'repo': task.repo, 'ref': task.revision, 'patterns': [task.filename]}}
            finish(step, 'completed')
        elif task.state in ('FAILED', 'CANCELLED'):
            finish(step, 'error' if task.state == 'FAILED' else 'cancelled', task.error)
    elif step.training_id:
        training = TrainingRun.objects.get(pk=step.training_id)
        while training.recovery_run_id:
            training = training.recovery_run
        step.message = training.get_state_display()
        if training.state == 'COMPLETED':
            checkpoint = training.checkpoints.order_by('-superbatch').first()
            artifact = checkpoint.network if checkpoint else training.artifacts.filter(kind='network').order_by('-pk').first()
            if artifact is None:
                raise ValidationError('Training completed without a network artifact.')
            network = imported_network(artifact, step)
            step.outputs = {'training': training.pk, 'artifact': artifact.pk, 'network': network.sha256, 'network_name': network.name, 'sha256': artifact.sha256, 'metrics': training.metrics}
            finish(step, 'completed')
        elif training.state in ('FAILED', 'CANCELLED'):
            if training.state == 'FAILED' and training.metrics.get('recovery_pending') and not training.cancel_requested:
                step.message = 'Waiting for worker reconnection and checkpoint recovery.'
                return
            finish(step, 'error' if training.state == 'FAILED' else 'cancelled', training.error)


def stop_jobs(step):
    if step.test_id:
        Test.objects.filter(pk=step.test_id, finished=False).update(finished=True)
    if step.training_id:
        training = TrainingRun.objects.get(pk=step.training_id)
        while training.recovery_run_id:
            training = training.recovery_run
        TrainingRun.objects.filter(pk=training.pk).exclude(state='COMPLETED').update(cancel_requested=True)
        TrainingRun.objects.filter(pk=training.pk, state__in=('VALIDATING', 'PREPARING', 'QUEUED')).update(state='CANCELLED', finished=timezone.now())
    if step.upload_id:
        DatasetUpload.objects.filter(pk=step.upload_id, state='QUEUED').update(state='CANCELLED')


def jobs_stopped(step):
    if step.upload_id and DatasetUpload.objects.filter(pk=step.upload_id, state='UPLOADING').exists():
        return False
    if step.training_id:
        training = TrainingRun.objects.get(pk=step.training_id)
        while training.recovery_run_id:
            training = training.recovery_run
        if training.state not in ('COMPLETED', 'FAILED', 'CANCELLED'):
            return False
    return True


def advance(run_id):
    with transaction.atomic():
        run = WorkflowRun.objects.select_for_update().select_related('workflow', 'owner').get(pk=run_id)
        if run.state not in ('RUNNING', 'PAUSED', 'CANCELLING'):
            return
        steps = {step.key: step for step in run.steps.select_for_update().select_related('run__workflow', 'run__owner')}
        nodes = {node['id']: node for node in run.snapshot['steps']}
        for key, step in steps.items():
            node = nodes[key]
            attempts_before = step.attempts
            launching = False
            try:
                if run.state == 'CANCELLING':
                    stop_jobs(step)
                    if not jobs_stopped(step):
                        step.message = 'Waiting for active jobs to stop before cancellation.'
                    else:
                        if step.state not in TERMINAL:
                            step.state, step.outcome, step.finished = 'CANCELLED', 'cancelled', timezone.now()
                elif step.state == 'RUNNING':
                    poll(step, node)
                if step.state in ('RUNNING', 'WAITING', 'PENDING') and step.started and (timezone.now() - step.started).total_seconds() > node['timeout_hours'] * 3600:
                    stop_jobs(step)
                    if step.state == 'PENDING':
                        step.attempts += 1
                    finish(step, 'error', 'Step deadline exceeded.')
                if run.state == 'RUNNING' and step.state == 'COMPLETED' and step.outcome == 'error' and step.attempts <= node['retries'] and jobs_stopped(step):
                    step.history = [*step.history, {'attempt': step.attempts, 'test': step.test_id, 'training': step.training_id, 'upload': step.upload_id, 'message': step.message}]
                    step.state, step.outcome, step.test, step.training, step.upload = 'PENDING', '', None, None, None
                    step.finished = None
                    step.started = None
                if run.state == 'RUNNING' and step.state == 'PENDING':
                    incoming = [edge for edge in run.snapshot['edges'] if edge['to'] == key]
                    ready = [steps[edge['from']].state in TERMINAL and not (steps[edge['from']].outcome == 'error' and steps[edge['from']].attempts <= nodes[edge['from']]['retries']) and jobs_stopped(steps[edge['from']]) for edge in incoming]
                    selected = [done and steps[edge['from']].outcome in edge['on'] for done, edge in zip(ready, incoming)]
                    active = not incoming or (all(selected) if node['join'] == 'all' else any(selected))
                    sources = [steps[source] for source in node['inputs'].values()]
                    if active and any(source.state in TERMINAL and not source.outputs for source in sources):
                        step.state, step.finished = 'SKIPPED', timezone.now()
                    elif active and all(source.state in TERMINAL for source in sources):
                        if step.started is None:
                            step.started = timezone.now()
                        launching = True
                        try:
                            with transaction.atomic():
                                launch(step, node, {name: row.outputs for name, row in steps.items()})
                        except Exception:
                            step.refresh_from_db()
                            raise
                    elif not active and all(ready):
                        step.state, step.finished = 'SKIPPED', timezone.now()
                step.updated = timezone.now()
                step.save()
            except (ValidationError, ValueError, KeyError) as error:
                stop_jobs(step)
                finish(step, 'error', str(error)[:2000])
                step.attempts = max(step.attempts, attempts_before + 1 if launching else max(1, attempts_before))
                step.save()
        retry_pending = run.state in ('RUNNING', 'PAUSED') and any(step.state == 'COMPLETED' and step.outcome == 'error' and step.attempts <= nodes[key]['retries'] for key, step in steps.items())
        stopping = any(not jobs_stopped(step) for step in steps.values() if step.state in TERMINAL)
        if all(step.state in TERMINAL for step in steps.values()) and not retry_pending and not stopping:
            if run.state == 'CANCELLING':
                run.state = 'CANCELLED'
            else:
                unhandled = [step for step in steps.values() if step.outcome in ('fail', 'error', 'inconclusive', 'cancelled') and not any(edge['from'] == step.key and step.outcome in edge['on'] for edge in run.snapshot['edges'])]
                run.state = 'FAILED' if unhandled else 'COMPLETED'
            run.finished = timezone.now()
        run.updated = timezone.now()
        run.save(update_fields=['state', 'updated', 'finished'])


def tick():
    for identifier in WorkflowRun.objects.filter(state__in=('RUNNING', 'PAUSED', 'CANCELLING')).order_by('updated').values_list('pk', flat=True)[:100]:
        advance(identifier)
