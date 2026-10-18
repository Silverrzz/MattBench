import json
import re
import uuid

from django.contrib.auth.decorators import login_required
from django.core.exceptions import ObjectDoesNotExist, PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import F
from django.http import FileResponse, JsonResponse
from django.shortcuts import get_object_or_404
from django.shortcuts import redirect
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from OpenBench.models import EngineConfig, Machine, TrainingProfile, TrainingWorker, Test, Workflow, WorkflowRun
from OpenBench.training_profiles import BUILTINS, default_spec, profile_data, profiles, validate_profile
from OpenBench.training_views import enabled, error_text, schedules_for
from OpenBench.workflows import KINDS, finish, start, validate_definition


def body(request):
    if len(request.body) > 2 * 1024 ** 2:
        raise ValidationError('Workflow definition exceeds 2 MiB.')
    value = json.loads(request.body)
    if not isinstance(value, dict):
        raise ValidationError('Invalid request.')
    return value


def example(profile='standard'):
    return {'profile': profile, 'engine': '', 'steps': [], 'edges': [], 'layout': {'nodes': {}, 'viewport': {'x': 0, 'y': 0, 'zoom': 1}}}


@login_required(login_url='/login/')
@require_http_methods(['GET', 'POST'])
def editor(request, identifier=None, create=False):
    from OpenBench.views import render
    enabled(request.user)
    row = get_object_or_404(Workflow, pk=identifier, owner=request.user) if identifier else None
    if request.method == 'POST':
        try:
            data = body(request)
            definition = validate_definition(data.get('definition'), draft=True)
            name = data.get('name', '').strip()
            if not name or len(name) > 128:
                raise ValidationError('Enter a workflow name of at most 128 characters.')
            if row:
                if type(data.get('version')) is not int or not Workflow.objects.filter(pk=row.pk, version=data['version']).update(name=name, definition=definition, version=F('version') + 1, updated=timezone.now()):
                    return JsonResponse({'error': 'This workflow changed. Reload before saving.'}, status=409)
                row.refresh_from_db()
            else:
                row = Workflow.objects.create(owner=request.user, name=name, definition=definition)
            return JsonResponse({'id': str(row.pk), 'version': row.version, 'url': '/workflows/%s/' % row.pk})
        except (ValidationError, ValueError, TypeError, AttributeError) as error:
            return JsonResponse({'error': error_text(error)}, status=400)
    if not row and not create:
        return render(request, 'workflows.html', {'page_title': 'Workflows', 'workflows': Workflow.objects.filter(owner=request.user, archived=False), 'runs': WorkflowRun.objects.filter(owner=request.user).select_related('workflow')[:50]})
    from OpenBench.schedule_builder import builder_state
    schedules = []
    for schedule in schedules_for(request.user):
        spec = builder_state(schedule)[0]
        schedules.append({'id': str(schedule.pk), 'name': schedule.name, 'profile': (schedule.settings.get('training_profile') or {}).get('id', (spec or {}).get('training_profile', 'standard')),
                          'engine': str(schedule.engine_id or ''), 'spec': spec})
    from OpenBench.models import Network, OpeningBook, TrainingArtifact, TrainingCheckpoint, TrainingDataset, WorkflowArtifact
    engine_rows = list(EngineConfig.objects.filter(enabled=True))
    from OpenBench.workflow_workloads import defaults
    engine_defaults = {str(engine.pk): {kind: defaults(engine, kind, 'standard') for kind in ('datagen', 'test', 'tune')} for engine in engine_rows}
    workers = [{'id': 'cpu:%d' % worker.pk, 'name': worker.info.get('machine_name') or 'CPU worker %d' % worker.pk} for worker in Machine.objects.filter(user=request.user).order_by('-updated')[:500]]
    workers += [{'id': 'gpu:' + str(worker.pk), 'name': worker.name + (' · CPU' if worker.info.get('backend') == 'cpu' else ' · GPU')} for worker in TrainingWorker.objects.filter(owner=request.user, enabled=True, machine__isnull=True)]
    from OpenBench.workflow_contracts import PORTS
    profile_rows = profiles()
    if not profile_rows:
        return redirect('/training/profiles/')
    default_profile = next((profile['id'] for profile in profile_rows if profile['id'] == 'standard'), profile_rows[0]['id'])
    return render(request, 'workflow_editor.html', {'page_title': row.name if row else 'New workflow', 'workflow_data': {
        'id': str(row.pk) if row else '', 'version': row.version if row else 0, 'name': row.name if row else '', 'definition': row.definition if row else example(default_profile),
        'profiles': [profile_data(profile) for profile in profile_rows], 'engines': [{'id': str(engine.pk), 'name': engine.name} for engine in engine_rows], 'schedules': schedules, 'workers': workers, 'engine_defaults': engine_defaults,
        'books': [{'id': book.name, 'name': book.name, 'variants': list(book.variants.values_list('name', flat=True))} for book in OpeningBook.objects.filter(enabled=True)],
        'networks': list(Network.objects.values('sha256', 'name', 'engine')),
        'ports': PORTS, 'schedule_defaults': {profile['id']: default_spec(profile) for profile in profile_rows},
        'datasets': [{'id': str(row.pk), 'name': row.name} for row in TrainingDataset.objects.filter(owner=request.user, archived=False)],
        'datagens': [{'id': row.pk, 'name': '#%d - %s' % (row.pk, row.info or row.dev_engine)} for row in Test.objects.filter(author=request.user.username, test_mode='DATAGEN', deleted=False).order_by('-pk')[:500]],
        'tunes': [{'id': row.pk, 'name': '#%d - %s' % (row.pk, row.info or row.dev_engine)} for row in Test.objects.filter(author=request.user.username, test_mode='SPSA', deleted=False).order_by('-pk')[:500]],
        'checkpoints': [{'id': row.pk, 'name': '%s - SB %d' % (row.run.name, row.superbatch)} for row in TrainingCheckpoint.objects.filter(run__owner=request.user).select_related('run')[:500]],
        'workflow_artifacts': [{'id': row.pk, 'name': '%s / %s' % (row.task.snapshot['node']['name'], row.name)} for row in WorkflowArtifact.objects.filter(task__owner=request.user).select_related('task').order_by('-pk')[:500]],
        'artifacts': [{'id': row.pk, 'name': '%s / %s' % (row.run.name, row.name)} for row in TrainingArtifact.objects.filter(run__owner=request.user).select_related('run').order_by('-pk')[:500]],
    }})


@login_required(login_url='/login/')
@require_http_methods(['POST'])
def launch(request, identifier):
    enabled(request.user)
    try:
        with transaction.atomic():
            workflow = get_object_or_404(Workflow.objects.select_for_update(), pk=identifier, owner=request.user)
            data = body(request)
            if data.get('version') != workflow.version:
                return JsonResponse({'error': 'Save and reload the current workflow version before running.'}, status=409)
            identifier = uuid.UUID(data.get("request_id", ""))
            run = WorkflowRun.objects.filter(pk=identifier, workflow=workflow, owner=request.user).first() or start(workflow, request.user, identifier)
        return JsonResponse({'url': '/workflows/runs/%s/' % run.pk})
    except IntegrityError:
        return JsonResponse({'error': 'This run identifier is already in use. Reload before running.'}, status=409)
    except (ValidationError, ValueError, TypeError, AttributeError) as error:
        return JsonResponse({'error': error_text(error)}, status=400)


def run_data(run):
    nodes = {node['id']: node for node in run.snapshot['steps']}
    steps = []
    for step in run.steps.all():
        node = nodes[step.key]
        url = '/training/%d/' % step.training_id if step.training_id else '/%s/%d/' % ('datagen' if node['kind'] == 'datagen' else 'test', step.test_id) if step.test_id else '/workflows/tasks/%d/' % step.task_id if step.task_id else ''
        steps.append({'id': step.key, 'name': node['name'], 'kind': node['kind'], 'state': step.state, 'outcome': step.outcome, 'attempts': step.attempts,
                      'message': step.message, 'outputs': step.outputs, 'history': step.history, 'url': url, 'worker': node['worker'], 'task': step.task_id})
    return {'id': str(run.pk), 'state': run.state, 'version': run.version, 'profile': run.snapshot['training_profile'], 'steps': steps, 'message': run.message}


@login_required(login_url='/login/')
@require_http_methods(['GET', 'POST'])
def detail(request, identifier):
    from OpenBench.views import render
    enabled(request.user)
    run = get_object_or_404(WorkflowRun.objects.select_related('workflow'), pk=identifier, owner=request.user)
    if request.method == 'POST':
        try:
            data = body(request)
            with transaction.atomic():
                run = WorkflowRun.objects.select_for_update().get(pk=run.pk)
                action = data.get('action')
                if action == 'pause' and run.state == 'RUNNING':
                    run.state = 'PAUSED'
                elif action == 'resume' and run.state == 'PAUSED':
                    run.state = 'RUNNING'
                elif action == 'cancel' and run.state in ('RUNNING', 'PAUSED'):
                    run.state = 'CANCELLING'
                elif action in ('approve', 'decline') and run.state in ('RUNNING', 'PAUSED'):
                    step = run.steps.select_for_update().get(key=data.get('step'), state='WAITING')
                    finish(step, 'pass' if action == 'approve' else 'fail', 'Decision recorded by workflow owner.')
                    step.save()
                else:
                    raise ValidationError('This workflow action is unavailable.')
                run.updated = timezone.now()
                run.save(update_fields=['state', 'updated'])
            return JsonResponse(run_data(run))
        except (ValidationError, ValueError, TypeError, ObjectDoesNotExist) as error:
            return JsonResponse({'error': error_text(error)}, status=400)
    if request.GET.get('format') == 'json':
        return JsonResponse(run_data(run))
    return render(request, 'workflow_run.html', {'page_title': run.workflow.name, 'run': run, 'run_data': run_data(run)})


@login_required(login_url='/login/')
@require_http_methods(['GET', 'POST'])
def training_profiles(request):
    from OpenBench.views import render
    enabled(request.user)
    error = ''
    if request.method == 'POST':
        if not request.user.is_superuser:
            raise PermissionDenied
        try:
            identifier = request.POST.get('id', '')
            name = request.POST.get('name', '').strip()
            if not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', identifier) or not name or len(name) > 128:
                raise ValidationError('Choose a profile identifier and name.')
            generator = request.POST.get('generator')
            from OpenBench.training_adapters import ADAPTERS, DATA_TOOLS
            if generator not in ADAPTERS:
                raise ValidationError('Choose a registered schedule adapter.')
            adapter = ADAPTERS[generator]
            value = {'schema': 2, 'generator': generator, 'variant': request.POST.get('variant', BUILTINS[generator]['variant']),
                     'bullet_repo': request.POST.get('bullet_repo', ''), 'bullet_ref': request.POST.get('bullet_ref', ''),
                     'features': request.POST.getlist('features') if 'schema' in request.POST else adapter['features'],
                     'optimizers': request.POST.getlist('optimizers') if 'schema' in request.POST else adapter['optimizers'],
                     'defaults': json.loads(request.POST.get('defaults', '{}')),
                     'training_settings': json.loads(request.POST.get('training_settings', '{}'))}
            if request.POST.get('rust_minimum'):
                value['rust_minimum'] = request.POST['rust_minimum']
            provider = request.POST.get('tool_kind', BUILTINS[generator]['data_tool']['kind'])
            value['data_tool'] = {'kind': provider, 'repo': request.POST.get('tool_repo', ''), 'commit': request.POST.get('tool_commit', '')}
            if DATA_TOOLS.get(provider, {}).get('combiner'):
                value['data_tool']['combiner'] = {'repo': request.POST.get('combiner_repo', BUILTINS['standard']['data_tool']['combiner']['repo']), 'commit': request.POST.get('combiner_commit', BUILTINS['standard']['data_tool']['combiner']['commit'])}
            value = validate_profile(value)
            from OpenBench.training import validate_schedule
            from OpenBench.training_profiles import profile_runtime
            candidate = {**value, 'id': identifier, 'name': name, 'version': int(request.POST.get('version', 0)) + 1}
            runtime = profile_runtime(candidate)
            validate_schedule({runtime['example_source']: 'fn main() {}'}, runtime)
            with transaction.atomic():
                current = TrainingProfile.objects.select_for_update().filter(pk=identifier).first()
                version = current.version if current else 1 if identifier in BUILTINS else 0
                if int(request.POST.get('version', 0)) != version:
                    raise ValidationError('This profile changed. Reload before saving.')
                TrainingProfile.objects.update_or_create(pk=identifier, defaults={'name': name, 'settings': value, 'version': version + 1, 'enabled': request.POST.get('enabled') == 'on'})
            return redirect('/training/profiles/')
        except (ValidationError, ValueError, TypeError) as exception:
            error = error_text(exception)
    available = profiles()
    disabled = [{**validate_profile(row.settings), 'id': row.pk, 'name': row.name, 'version': row.version, 'disabled': True} for row in TrainingProfile.objects.filter(enabled=False)]
    available = [profile_data(profile) for profile in available]
    disabled = [profile_data(profile) for profile in disabled]
    for profile in [*available, *disabled]:
        profile['feature_labels'] = [feature['short'] for feature in profile['input_features']]
        profile['source_label'] = profile['bullet_repo'].removeprefix('https://github.com/').replace('/', ' / ')
    from OpenBench.training_adapters import ADAPTERS, DATA_TOOLS, INPUT_FEATURES
    return render(request, 'training_profiles.html', {'page_title': 'Training profiles', 'profiles': available, 'disabled_profiles': disabled, 'form_error': error,
        'profile_adapters': [{'id': key, 'label': value['label']} for key, value in ADAPTERS.items()],
        'profile_features': [{'key': key, **value} for key, value in INPUT_FEATURES.items()],
        'profile_tools': [{'id': key, **value} for key, value in DATA_TOOLS.items()],
        'profile_editor_data': {'profiles': [*available, *disabled], 'templates': {key: profile_data({**value, 'id': key}) for key, value in BUILTINS.items()},
                                'tools': DATA_TOOLS, 'error': error, 'submitted': {**request.POST.dict(), 'features': request.POST.getlist('features'), 'optimizers': request.POST.getlist('optimizers')} if error else {}}})


@login_required(login_url='/login/')
@require_http_methods(['GET'])
def task_detail(request, pk, artifact_id=None):
    from OpenBench.models import WorkflowTask
    from OpenBench.training_storage import artifact_path
    from OpenBench.views import render
    enabled(request.user)
    task = get_object_or_404(WorkflowTask.objects.select_related('worker'), pk=pk, owner=request.user)
    if artifact_id:
        artifact = get_object_or_404(task.artifacts, pk=artifact_id)
        response = FileResponse(artifact_path(artifact.path).open('rb'), as_attachment=True, filename=artifact.name)
        response['X-Checksum-SHA256'] = artifact.sha256
        return response
    return render(request, 'workflow_task.html', {'page_title': task.snapshot['node']['name'], 'task': task,
        'workflow_run': task.snapshot['workflow']['run'], 'configuration': json.dumps(task.snapshot['node']['config'], indent=2), 'outputs': json.dumps(task.outputs, indent=2)})
