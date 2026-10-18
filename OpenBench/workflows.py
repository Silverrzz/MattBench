import copy
import hashlib
import json
import math
import re
from functools import lru_cache
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.core.files import File
from django.core.files.storage import FileSystemStorage
from django.db import transaction
from django.utils import timezone

from OpenBench.models import DatasetUpload, Engine, EngineConfig, Network, Profile, Test, TrainingArtifact, TrainingRun, TrainingSchedule, Workflow, WorkflowRun, WorkflowStep
from OpenBench.worker_policy import validate_policy


from OpenBench.workflow_contracts import PORTS, inputs as asset_inputs, ports, publish, ref, references


KINDS = tuple(PORTS)
OUTCOMES = ('completed', 'pass', 'fail', 'inconclusive', 'error', 'cancelled')
TERMINAL = ('COMPLETED', 'SKIPPED', 'CANCELLED')
TEST_FIELDS = ('book_name', 'dev_repo', 'dev_engine', 'dev_options', 'dev_network', 'dev_netname', 'dev_time_control',
               'base_repo', 'base_engine', 'base_options', 'base_network', 'base_netname', 'base_time_control',
               'workload_size', 'priority', 'throughput', 'scale_method', 'scale_nps', 'syzygy_wdl', 'syzygy_adj',
               'win_adj', 'draw_adj', 'test_mode', 'elolower', 'eloupper', 'alpha', 'beta', 'lowerllr', 'upperllr',
               'max_games', 'genfens_args', 'play_reverses', 'use_tri', 'use_penta', 'info')


def validate_definition(value, draft=False):
    if not isinstance(value, dict) or set(value) - {'profile', 'engine', 'steps', 'edges', 'layout'}:
        raise ValidationError('Invalid workflow definition.')
    from OpenBench.training_profiles import resolve_profile
    resolve_profile(value.get('profile', 'standard'))
    if value.get('engine') and not EngineConfig.objects.filter(pk=value.get('engine'), enabled=True).exists():
        raise ValidationError('Choose an enabled engine.')
    steps, edges = value.get('steps'), value.get('edges', [])
    if not isinstance(steps, list) or not (0 if draft else 1) <= len(steps) <= 64 or not isinstance(edges, list) or len(edges) > 256:
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
        shown = step.get('config', {}).get('outcome_ports', [])
        if not isinstance(shown, list) or len(shown) > len(OUTCOMES) or any(not isinstance(outcome, str) or outcome not in OUTCOMES for outcome in shown):
            raise ValidationError('Choose valid outcome ports.')
        retries, timeout = step.get('retries', 0), step.get('timeout_hours', 168)
        if type(retries) is not int or not 0 <= retries <= 10 or type(timeout) is not int or not 1 <= timeout <= 8760:
            raise ValidationError('Use 0–10 retries and a deadline of 1–8760 hours.')
        normalized.append({**step, 'config': step.get('config', {}), 'inputs': step.get('inputs', {}), 'worker': validate_policy(step.get('worker', {})), 'join': step.get('join', 'all'), 'retries': retries, 'timeout_hours': timeout})
    graph = {key: [] for key in keys}
    connections = []
    for edge in edges:
        if not isinstance(edge, dict) or set(edge) - {'from', 'to', 'on', 'output', 'input'} or not {'from', 'to', 'on'} <= set(edge) or edge['from'] not in keys or edge['to'] not in keys or edge['from'] == edge['to']:
            raise ValidationError('Connect existing, different steps.')
        if not isinstance(edge['on'], list) or not edge['on'] or set(edge['on']) - set(OUTCOMES):
            raise ValidationError('Choose valid connection outcomes.')
        if any((e['from'], e['to'], e.get('output'), e.get('input')) == (edge['from'], edge['to'], edge.get('output'), edge.get('input')) for e in connections):
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
    wired = {}
    for edge in connections:
        if 'output' in edge or 'input' in edge:
            target = by_key[edge['to']]
            available = ports(target)[0]
            if edge.get('input') not in available:
                raise ValidationError('Choose an existing input port.')
            reference = ref({'node': edge['from'], 'port': edge.get('output')}, by_key, available[edge['input']])
            wired.setdefault((target['id'], edge['input']), []).append(reference)
    for step in normalized:
        parents = ancestors(step['id'])
        available = ports(step)[0]
        for name in available:
            connected = wired.get((step['id'], name))
            if connected:
                if '[]' not in available[name] and len(connected) > 1:
                    raise ValidationError('This input accepts one source. Use a merge node for multiple datasets.')
                step['inputs'][name] = connected if '[]' in available[name] else connected[0]
        for name, source in list(step['inputs'].items()):
            if name not in available:
                raise ValidationError('Choose a supported input port.')
            values = references(source)
            if not values or len(values) > 256 or isinstance(source, list) and '[]' not in available[name]:
                raise ValidationError('Invalid number of input connections.')
            values = [ref(item, by_key, available[name]) for item in values]
            if any(item['node'] not in parents for item in values):
                raise ValidationError('Step inputs must come from connected upstream steps.')
            step['inputs'][name] = values if '[]' in available[name] else values[0]
        if not draft:
            for name, kind in available.items():
                if not kind.endswith('?') and name not in step['inputs']:
                    raise ValidationError(step['name'] + ' needs an upstream ' + name + ' connection.')
    layout = value.get('layout', {})
    if not isinstance(layout, dict) or set(layout) - {'nodes', 'viewport'} or not isinstance(layout.get('nodes', {}), dict):
        raise ValidationError('Invalid canvas layout.')
    positions = layout.get('nodes', {})
    viewport = layout.get('viewport', {'x': 0, 'y': 0, 'zoom': 1})
    if set(positions) - keys or not isinstance(viewport, dict) or set(viewport) != {'x', 'y', 'zoom'}:
        raise ValidationError('Invalid canvas layout.')
    for point in [*positions.values(), viewport]:
        if not isinstance(point, dict) or (set(point) != {'x', 'y', 'zoom'} if point is viewport else set(point) != {'x', 'y'}) or any(type(number) not in (int, float) or not math.isfinite(number) or abs(number) > 10000000 for number in point.values()):
            raise ValidationError('Invalid canvas position.')
    if not 0.15 <= viewport['zoom'] <= 2:
        raise ValidationError('Invalid canvas zoom.')
    return {'profile': value.get('profile', 'standard'), 'engine': str(value.get('engine') or ''), 'steps': normalized, 'edges': connections, 'layout': {'nodes': positions, 'viewport': viewport}}


def workload_snapshot(test):
    values = {name: copy.deepcopy(getattr(test, name)) for name in TEST_FIELDS}
    values['execution'] = {key: copy.deepcopy(value) for key, value in test.execution.items() if key not in ('demo', 'workflow', 'worker_policy', 'progress', 'pending_progress', 'llr_allocation', 'automatic_bench', 'bench_discoveries', 'bench_options', 'bench_method')}
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
    from OpenBench.workflow_prepare import prepare
    for node in definition['steps']:
        prepare(node, definition, user)
    run = WorkflowRun.objects.create(**({"id": identifier} if identifier else {}), workflow=workflow, owner=user, version=workflow.version, snapshot=definition)
    WorkflowStep.objects.bulk_create([WorkflowStep(run=run, key=node['id']) for node in definition['steps']])
    from OpenBench.models import WorkflowAssetPin
    for node in definition['steps']:
        if node['kind'] == 'load_checkpoint':
            WorkflowAssetPin.objects.get_or_create(run=run, checkpoint_id=node['config']['checkpoint'])
        elif node['kind'] == 'load_artifact' and node['config'].get('provider', 'training') == 'training':
            WorkflowAssetPin.objects.get_or_create(run=run, artifact_id=node['config']['artifact'])
        elif node['kind'] == 'train' and node['config'].get('training_options', {}).get('checkpoint'):
            WorkflowAssetPin.objects.get_or_create(run=run, checkpoint_id=node['config']['training_options']['checkpoint'])
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
    inputs = asset_inputs(node, outputs)
    if node['kind'] in ('datagen', 'test', 'tune'):
        values = copy.deepcopy(config['workload'])
        for side in ('dev', 'base'):
            values[side] = Engine.objects.create(**values[side])
        values.update(author=run.owner.username, info=values.get('info') or 'Workflow %s · %s' % (run.workflow.name, node['name']),
                      execution={**values['execution'], 'workflow': reference(step), 'worker_policy': node['worker'], 'bench_method': 'command-line'})
        approval = Profile.objects.get(user=run.owner)
        from OpenBench.config import OPENBENCH_CONFIG
        values['approved'] = approval.approver and not OPENBENCH_CONFIG['use_cross_approval']
        shared = ('dev', 'base') if node['kind'] == 'tune' else ('dev',)
        for name, sides in (('network', shared), ('base_network', ('base',))):
            if name not in inputs:
                continue
            candidate = inputs[name]
            network = Network.objects.filter(sha256=candidate['network']).first()
            if not network or any(network.engine != values[side + '_engine'] for side in sides):
                raise ValidationError('The network output belongs to a different engine.')
            for side in sides:
                values.update({side + '_network': network.sha256, side + '_netname': network.name})
                values[side].bench = 0
                values[side].save(update_fields=['bench'])
                values['execution'].setdefault('automatic_bench', []).append(side)
        from OpenBench.workflow_contracts import apply_parameters
        for name, sides in (('parameters', shared), ('base_parameters', ('base',))):
            if name in inputs:
                for side in sides:
                    values[side + '_options'] = apply_parameters(values[side + '_options'], inputs[name])
                    from OpenBench.utils import extract_option
                    if any(int(extract_option(values[side + '_options'], option)) < 1 for option in ('Threads', 'Hash')):
                        raise ValidationError('Workflow parameters must keep Threads and Hash positive.')
                    values[side].bench = 0
                    values[side].save(update_fields=['bench'])
                    automatic = values['execution'].setdefault('automatic_bench', [])
                    if side not in automatic:
                        automatic.append(side)
                    values['execution'].setdefault('bench_options', {})[side] = values[side + '_options']
        if node['kind'] == 'datagen' and 'parameters' not in config:
            values.update(max_games=config.get('games', 10000000), upload_pgns='COMPACT')
        elif node['kind'] == 'test':
            control = config.get('control', 'template')
            if control != 'template' and 'parameters' not in config:
                tc = 'N=%d' % config['nodes'] if control == 'nodes' else config.get('time_control') or ('10.0+0.1' if control == 'stc' else '60.0+0.6')
                from OpenBench.utils import TimeControl
                tc = TimeControl.parse(tc)
                values.update(dev_time_control=tc, base_time_control=tc)
            values['execution']['workflow_game_cap'] = config.get('max_games', 0)
        step.test = Test.objects.create(**values)
        if node['kind'] == 'tune':
            from types import SimpleNamespace
            from OpenBench.spsa_utils import create_spsa_run
            options = copy.deepcopy(config['spsa'])
            if inputs.get('parameters'):
                initial = inputs['parameters']['values']
                rows = []
                for line in options['spsa_inputs'].splitlines():
                    cells = [cell.strip() for cell in line.split(',')]
                    if cells[0] in initial:
                        value = initial[cells[0]]
                        if not float(cells[3]) <= value <= float(cells[4]):
                            raise ValidationError('Initial parameter is outside the tune bounds: ' + cells[0])
                        cells[2] = str(value)
                    rows.append(', '.join(cells))
                options['spsa_inputs'] = '\n'.join(rows)
            create_spsa_run(step.test, SimpleNamespace(POST=options))
        from OpenBench.lifecycle import test_event
        test_event('created', step.test, run.owner_id)
        if values['approved']:
            test_event('approved', step.test, run.owner_id)
    elif node['kind'] == 'train':
        from OpenBench.workflow_training import launch as training
        snapshot, dataset, resume, options = training(config, inputs, run.owner)
        snapshot.update(workflow=reference(step), worker_policy=node['worker'])
        step.training = TrainingRun.objects.create(owner=run.owner, engine_id=config.get('engine') or run.snapshot['engine'], name=options.get('name') or 'wf-%s-%s' % (str(run.pk)[:8], step.key),
            snapshot=snapshot, dataset=dataset, parameters=inputs.get('parameters', {}).get('values', {}), resume_from=resume, priority=options.get('priority', config.get('priority', 0)),
            workload_size=options.get('workload_size', 0), completed_superbatches=resume.superbatch if resume else 0)
    elif node['kind'] in ('convert', 'split', 'task', 'upload'):
        from OpenBench.workflow_tasks import enqueue
        enqueue(step, node, inputs)
    elif node['kind'] == 'approval':
        step.state = 'WAITING'
        step.message = 'Waiting for the workflow owner’s decision.'
    elif node['kind'] == 'decision':
        import operator
        source = inputs['source']
        metric = source.get('values', source.get('metrics', source))
        for part in config['field'].split('.'):
            metric = metric.get(part) if isinstance(metric, dict) else None
        if type(metric) not in (int, float) or not math.isfinite(metric):
            raise ValidationError('The selected metric is unavailable.')
        result = getattr(operator, {'gt': 'gt', 'gte': 'ge', 'lt': 'lt', 'lte': 'le', 'eq': 'eq'}[config['operator']])(metric, config['value'])
        publish(step, metrics=source)
        finish(step, 'pass' if result else 'fail')
    else:
        from OpenBench import workflow_assets as assets
        if node['kind'] == 'load_data':
            asset = assets.dataset_source(config, run.owner)
            if any(source.get('provider') == 'datagen' and not Test.objects.filter(pk=source['workload'], finished=True).exists() for source in asset['sources']):
                step.message = 'Waiting for the source datagen workload.'
                return False
            publish(step, data=assets.resolve_data(asset, run.owner))
        elif node['kind'] == 'load_parameters':
            publish(step, parameters=assets.parameters_source(config, run.owner))
        elif node['kind'] == 'load_network':
            publish(step, network=assets.network_source(config))
        elif node['kind'] == 'load_checkpoint':
            asset = assets.checkpoint_source(config, run.owner)
            from OpenBench.models import TrainingCheckpoint
            checkpoint = TrainingCheckpoint.objects.select_related('network').get(pk=asset['checkpoint'])
            network = imported_network(checkpoint.network, step)
            publish(step, checkpoint=asset, network={'type': 'network', 'network': network.sha256, 'network_name': network.name, 'engine': network.engine})
        elif node['kind'] == 'load_artifact':
            publish(step, artifact=assets.training_artifact(config, run.owner))
        elif node['kind'] == 'merge':
            sources = [source for asset in inputs['data'] for source in asset['sources']]
            unique = {json.dumps(source, sort_keys=True): source for source in sources}
            publish(step, data={'type': 'dataset', 'sources': list(unique.values())})
        elif node['kind'] == 'select':
            selected, remaining = assets.select_data(assets.resolve_data(inputs['data'], run.owner), config.get('patterns', ['*']))
            publish(step, matched=selected, remaining=remaining)
        finish(step, 'completed')
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
        if test.deleted or test.finished and test.error:
            finish(step, 'cancelled' if test.deleted else 'error', 'The workload was deleted.' if test.deleted else 'The workload stopped with an execution error.')
            return
        if test.finished and node['kind'] in ('datagen', 'tune'):
            target = test.max_games if node['kind'] == 'datagen' else 2 * test.spsa_run.pairs_per * test.spsa_run.iterations
            if test.games < target:
                finish(step, 'cancelled', 'The workload stopped before reaching its configured target.')
                return
        if test.finished and node['kind'] == 'datagen' and test.games >= test.max_games:
            from OpenBench.utils import getRecentMachines
            archive = Path(settings.MEDIA_ROOT) / 'PGNs' / ('%d.pgn.tar' % test.pk)
            if not archive.is_file() or getRecentMachines().filter(workload=test.pk).exists() or timezone.now().timestamp() - archive.stat().st_mtime < 30:
                step.message = 'Waiting for final workers and PGN archive assembly.'
                return
        if test.deleted or test.finished:
            step.outputs = {'test': test.pk, 'games': test.games, 'llr': test.currentllr}
            if node['kind'] == 'test':
                step.outputs.update(network=test.dev_network, network_name=test.dev_netname)
            publish(step, metrics={'type': 'metrics', 'values': {'games': test.games, 'llr': test.currentllr}})
            if node['kind'] == 'datagen':
                publish(step, data={'type': 'dataset', 'sources': [{'provider': 'datagen', 'workload': test.pk}]})
            elif node['kind'] == 'tune':
                from OpenBench.workflow_assets import parameters_from_tune
                publish(step, parameters=parameters_from_tune(test))
            elif test.dev_network:
                publish(step, network={'type': 'network', 'network': test.dev_network, 'network_name': test.dev_netname, 'engine': test.dev_engine})
            finish(step, 'cancelled' if test.deleted else ('completed' if node['kind'] == 'tune' or node['kind'] == 'datagen' and test.games >= test.max_games else 'pass' if test.passed else 'fail' if test.failed else 'inconclusive'))
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
            artifact = training.artifacts.filter(kind='network').order_by('-pk').first()
            if artifact is None:
                raise ValidationError('Training completed without a network artifact.')
            network = imported_network(artifact, step)
            step.outputs = {'training': training.pk, 'artifact': artifact.pk, 'network': network.sha256, 'network_name': network.name, 'sha256': artifact.sha256, 'metrics': training.metrics}
            publish(step, network={'type': 'network', 'network': network.sha256, 'network_name': network.name, 'engine': network.engine}, metrics={'type': 'metrics', 'values': training.metrics})
            if checkpoint:
                from OpenBench.workflow_assets import checkpoint_source
                publish(step, checkpoint=checkpoint_source({'checkpoint': checkpoint.pk}, step.run.owner))
            finish(step, 'completed')
        elif training.state in ('FAILED', 'CANCELLED'):
            if training.state == 'FAILED' and training.metrics.get('recovery_pending') and not training.cancel_requested:
                step.message = 'Waiting for worker reconnection and checkpoint recovery.'
                return
            finish(step, 'error' if training.state == 'FAILED' else 'cancelled', training.error)
    elif step.task_id:
        from OpenBench.models import WorkflowTask
        task = WorkflowTask.objects.get(pk=step.task_id)
        step.message = task.metrics.get('preparation_step') or task.state.title()
        if task.state == 'COMPLETED':
            publish(step, **task.outputs)
            finish(step, 'completed')
        elif task.state in ('FAILED', 'CANCELLED'):
            finish(step, 'error' if task.state == 'FAILED' else 'cancelled', task.error)



def stop_jobs(step):
    if step.task_id:
        from OpenBench.models import WorkflowTask
        WorkflowTask.objects.filter(pk=step.task_id).exclude(state__in=('COMPLETED', 'FAILED', 'CANCELLED')).update(cancel_requested=True)
        WorkflowTask.objects.filter(pk=step.task_id, state='QUEUED').update(state='CANCELLED', finished=timezone.now())
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
    if step.test_id:
        from OpenBench.utils import getRecentMachines
        if Test.objects.filter(pk=step.test_id, finished=False, deleted=False).exists() or getRecentMachines().filter(workload=step.test_id).exists():
            return False
    if step.task_id:
        from OpenBench.models import WorkflowTask
        if WorkflowTask.objects.filter(pk=step.task_id, state__in=('QUEUED', 'RUNNING')).exists():
            return False
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
                    step.history = [*step.history, {'attempt': step.attempts, 'test': step.test_id, 'training': step.training_id, 'upload': step.upload_id, 'task': step.task_id, 'message': step.message}]
                    step.state, step.outcome, step.test, step.training, step.upload = 'PENDING', '', None, None, None
                    step.task = None
                    step.outputs = {}
                    step.finished = None
                    step.started = None
                if run.state == 'RUNNING' and step.state == 'PENDING':
                    incoming = [edge for edge in run.snapshot['edges'] if edge['to'] == key]
                    def settled(parent):
                        row = steps[parent]
                        return row.state in TERMINAL and not (row.outcome == 'error' and row.attempts <= nodes[parent]['retries']) and jobs_stopped(row)
                    gates = [edge for edge in incoming if not edge.get('output')] or incoming
                    ready = [settled(edge['from']) for edge in gates]
                    selected = [done and steps[edge['from']].outcome in edge['on'] for done, edge in zip(ready, gates)]
                    active = not gates or (all(selected) if node['join'] == 'all' else any(selected))
                    bound = copy.deepcopy(node)
                    bound['inputs'] = {}
                    waiting, missing, unavailable = False, False, []
                    from OpenBench.workflow_contracts import read
                    all_outputs = {name: row.outputs for name, row in steps.items()}
                    for name, source in node['inputs'].items():
                        available = []
                        for item in references(source):
                            parent = item if isinstance(item, str) else item['node']
                            if not settled(parent):
                                waiting = True
                                continue
                            routes = [edge for edge in incoming if edge.get('output') and edge['from'] == parent and edge.get('input') == name and (isinstance(item, str) or edge['output'] == item['port'])]
                            if routes and not any(steps[parent].outcome in edge['on'] for edge in routes):
                                continue
                            try:
                                read(item, all_outputs)
                                available.append(item)
                            except ValidationError:
                                unavailable.append(parent)
                        if available:
                            bound['inputs'][name] = available if isinstance(source, list) else available[0]
                        else:
                            missing = True
                    if active and not waiting and unavailable:
                        raise ValidationError('A selected upstream step did not produce its connected output: ' + ', '.join(sorted(set(unavailable))))
                    can_launch = active and not waiting and not missing
                    if can_launch:
                        if step.started is None:
                            step.started = timezone.now()
                        launching = True
                        try:
                            with transaction.atomic():
                                launch(step, bound, all_outputs)
                        except Exception:
                            step.refresh_from_db()
                            raise
                    elif active and not waiting and missing:
                        step.state, step.finished = 'SKIPPED', timezone.now()
                    elif not active and all(ready):
                        step.state, step.finished = 'SKIPPED', timezone.now()
                step.updated = timezone.now()
                step.save()
            except (ValidationError, ObjectDoesNotExist, ValueError, KeyError, TypeError) as error:
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
                unhandled = [step for step in steps.values() if step.outcome in ('fail', 'error', 'inconclusive', 'cancelled') and not any(edge['from'] == step.key and step.outcome in edge['on'] and steps[edge['to']].attempts for edge in run.snapshot['edges'])]
                run.state = 'FAILED' if unhandled else 'COMPLETED'
            run.finished = timezone.now()
        run.updated = timezone.now()
        run.save(update_fields=['state', 'updated', 'finished'])


def tick():
    from OpenBench.workflow_tasks import expire
    expire()
    for identifier in WorkflowRun.objects.filter(state__in=('RUNNING', 'PAUSED', 'CANCELLING')).order_by('updated').values_list('pk', flat=True)[:100]:
        advance(identifier)
