import json
import re
import uuid

from django.contrib.auth.decorators import login_required
from django.core.exceptions import ObjectDoesNotExist, PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import F
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from OpenBench.models import EngineConfig, Machine, TrainingProfile, TrainingWorker, Test, Workflow, WorkflowRun
from OpenBench.training_profiles import BUILTINS, profiles, validate_profile
from OpenBench.training_views import enabled, error_text, schedules_for
from OpenBench.workflows import KINDS, finish, start, validate_definition


def body(request):
    if len(request.body) > 2 * 1024 ** 2:
        raise ValidationError('Workflow definition exceeds 2 MiB.')
    value = json.loads(request.body)
    if not isinstance(value, dict):
        raise ValidationError('Invalid request.')
    return value


def example():
    steps = []
    previous = None
    edges = []
    for key, name, kind, config in (
        ('generate', 'Generate 10M games', 'datagen', {'games': 10000000, 'template': ''}),
        ('upload', 'Upload dataset', 'upload', {'repo': '', 'private': True}),
        ('train', 'Train candidate', 'train', {'schedule': ''}),
        ('nodes', 'Fixed-node SPRT', 'test', {'template': '', 'control': 'nodes', 'nodes': 25000}),
        ('stc', 'STC SPRT', 'test', {'template': '', 'control': 'stc', 'time_control': '10.0+0.1'}),
        ('ltc', 'LTC SPRT', 'test', {'template': '', 'control': 'ltc', 'time_control': '60.0+0.6'}),
    ):
        inputs = {'dataset': previous['id']} if kind in ('upload', 'train') else {'network': 'train'} if kind == 'test' else {}
        steps.append({'id': key, 'name': name, 'kind': kind, 'config': config, 'inputs': inputs, 'worker': {}, 'join': 'all', 'retries': 0, 'timeout_hours': 168})
        if previous:
            edges.append({'from': previous['id'], 'to': key, 'on': ['pass' if previous['kind'] == 'test' else 'completed']})
        previous = steps[-1]
    return {'profile': 'standard', 'engine': '', 'steps': steps, 'edges': edges}


@login_required(login_url='/login/')
@require_http_methods(['GET', 'POST'])
def editor(request, identifier=None, create=False):
    from OpenBench.views import render
    enabled(request.user)
    row = get_object_or_404(Workflow, pk=identifier, owner=request.user) if identifier else None
    if request.method == 'POST':
        try:
            data = body(request)
            definition = validate_definition(data.get('definition'))
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
    schedules = [{'id': str(schedule.pk), 'name': schedule.name, 'profile': (builder_state(schedule)[0] or {}).get('training_profile', 'standard'), 'engine': str(schedule.engine_id or '')} for schedule in schedules_for(request.user)]
    templates = [{'id': test.pk, 'name': '#%d · %s · %s' % (test.pk, test.dev_engine, test.dev.name), 'kind': 'datagen' if test.test_mode == 'DATAGEN' else 'test', 'engine': test.dev_engine, 'variant': test.execution.get('fastchess_variant', 'standard')} for test in Test.objects.filter(deleted=False, test_mode__in=('DATAGEN', 'SPRT')).exclude(execution__has_key='demo').select_related('dev').order_by('-id')[:1000]]
    workers = [{'id': 'cpu:%d' % worker.pk, 'name': worker.info.get('machine_name') or 'CPU worker %d' % worker.pk} for worker in Machine.objects.filter(user=request.user).order_by('-updated')[:500]]
    workers += [{'id': 'gpu:' + str(worker.pk), 'name': worker.name + ' · GPU'} for worker in TrainingWorker.objects.filter(owner=request.user, enabled=True)]
    return render(request, 'workflow_editor.html', {'page_title': row.name if row else 'New workflow', 'workflow_data': {
        'id': str(row.pk) if row else '', 'version': row.version if row else 0, 'name': row.name if row else '', 'definition': row.definition if row else example(),
        'profiles': profiles(), 'engines': list(EngineConfig.objects.filter(enabled=True).values('id', 'name')), 'schedules': schedules, 'templates': templates, 'workers': workers,
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
        url = '/training/%d/' % step.training_id if step.training_id else '/%s/%d/' % ('datagen' if node['kind'] == 'datagen' else 'test', step.test_id) if step.test_id else ''
        steps.append({'id': step.key, 'name': node['name'], 'kind': node['kind'], 'state': step.state, 'outcome': step.outcome, 'attempts': step.attempts,
                      'message': step.message, 'outputs': step.outputs, 'history': step.history, 'url': url, 'worker': node['worker']})
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
            value = {'generator': generator, 'bullet_repo': request.POST.get('bullet_repo', ''), 'bullet_ref': request.POST.get('bullet_ref', '')}
            if generator == 'duckchess':
                value['data_tool'] = {'repo': request.POST.get('tool_repo', ''), 'commit': request.POST.get('tool_commit', '')}
            value = validate_profile(value)
            with transaction.atomic():
                current = TrainingProfile.objects.select_for_update().filter(pk=identifier).first()
                version = current.version if current else 1 if identifier in BUILTINS else 0
                if int(request.POST.get('version', 0)) != version:
                    raise ValidationError('This profile changed. Reload before saving.')
                TrainingProfile.objects.update_or_create(pk=identifier, defaults={'name': name, 'settings': value, 'version': version + 1, 'enabled': request.POST.get('enabled') == 'on'})
        except (ValidationError, ValueError, TypeError) as exception:
            error = error_text(exception)
    available = profiles()
    disabled = TrainingProfile.objects.filter(enabled=False)
    return render(request, 'training_profiles.html', {'page_title': 'Training profiles', 'profiles': available, 'disabled_profiles': disabled, 'form_error': error})
