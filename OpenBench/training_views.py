import json
import re
from datetime import timedelta
from pathlib import Path

from django import forms
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import F, Q
from django.http import FileResponse, JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST
from django.views.decorators.debug import sensitive_post_parameters

from OpenBench.models import DatasetUpload, EngineConfig, HuggingFaceCredential, Network, PGN, Profile
from OpenBench.models import Machine, Test, TrainingArtifact, TrainingRun, TrainingSchedule, TrainingWorker
from OpenBench.models import LifecycleEvent, TrainingDataset, TrainingCheckpoint
from OpenBench.lifecycle import record_event
from OpenBench.training import DEFAULT_SETTINGS, hf_token, relative_path, repo_id, save_credential, validate_schedule
from OpenBench.training import worker_requirement_errors
from OpenBench.training_models import TRAINING_ACTIVE, TRAINING_TERMINAL


def enabled(user):
    if not user.is_authenticated or not user.is_active or not Profile.objects.filter(user=user, enabled=True).exists():
        raise PermissionDenied


def schedules_for(user):
    return TrainingSchedule.objects.filter(Q(owner=user) | Q(owner=None)).select_related('engine', 'owner')


def visible_runs(user):
    rows = TrainingRun.objects.select_related('owner', 'engine', 'worker')
    return rows if user.is_superuser else rows.filter(owner=user)


def may_manage(user, run):
    return user.is_authenticated and user.is_active and (user.pk == run.owner_id or user.is_superuser)


def error_text(error):
    if isinstance(error, ValidationError):
        return '; '.join(error.messages)
    return 'Invalid input. Check the fields and try again.'


@login_required(login_url='/login/')
@require_http_methods(['GET'])
def training_index(request, page=1):
    from OpenBench.utils import getPaging
    from OpenBench.views import render
    runs = visible_runs(request.user).filter(deleted=False).select_related(None).select_related('owner', 'engine', 'worker', 'requested_worker').defer('snapshot', 'dataset', 'parameters', 'history', 'log_tail').annotate(backend=F('snapshot__settings__backend'), min_vram_gb=F('snapshot__settings__min_vram_gb'), demo=F('snapshot__demo'))
    finished = runs.filter(state__in=TRAINING_TERMINAL).order_by('-finished', '-pk')
    page = max(1, int(page))
    start, end, paging = getPaging(finished, page, 'training/page')
    paging['label'] = 'Training pages'
    return render(request, 'training_index.html', {
        'page_title': 'Training',
        'training_tab': 'runs', 'engines': EngineConfig.objects.filter(enabled=True).order_by('name'),
        'active': runs.exclude(state__in=TRAINING_TERMINAL).order_by('-created') if page == 1 else [],
        'finished': finished[start:end],
        'paging': paging,
    })


@login_required(login_url='/login/')
@sensitive_post_parameters('token')
@require_POST
def connection(request):
    from OpenBench.views import redirect
    enabled(request.user)
    try:
        if request.POST.get('action') == 'disconnect':
            HuggingFaceCredential.objects.filter(user=request.user).delete()
            return redirect(request, '/profile/', status='Hugging Face disconnected')
        save_credential(request.user, request.POST.get('token', '').strip())
    except ValidationError as error:
        return redirect(request, '/profile/', error=error_text(error))
    return redirect(request, '/profile/', status='Hugging Face connected')


@login_required(login_url='/login/')
@require_http_methods(['GET', 'POST'])
def schedules(request, schedule_id=None, create=False):
    from OpenBench.views import render
    enabled(request.user)
    selected = get_object_or_404(schedules_for(request.user), pk=schedule_id) if schedule_id else None
    if request.method == 'POST':
        try:
            if len(request.body) > 1500000:
                raise ValidationError('Schedules must total less than 1 MB.')
            data = json.loads(request.body)
            if not isinstance(data, dict):
                raise ValidationError('Invalid schedule.')
            if selected and not (selected.owner_id == request.user.pk or selected.owner_id is None and request.user.is_superuser):
                raise PermissionDenied
            if selected and data.get('action') == 'delete':
                if selected.version != int(data.get('version', 0)):
                    return JsonResponse({'error': 'This schedule changed in another tab. Reload before deleting.'}, status=409)
                selected.delete()
                return JsonResponse({'url': '/training/schedules/'})
            scope = data.get('scope', 'personal')
            if scope not in ('personal', 'engine', 'global'):
                raise ValidationError('Choose a schedule scope.')
            owner = request.user if scope == 'personal' else None
            if owner is None and not request.user.is_superuser:
                raise PermissionDenied
            engine = None if scope != 'engine' else get_object_or_404(EngineConfig, pk=data.get('engine'), enabled=True)
            name = str(data.get('name', '')).strip()
            if not name or len(name) > 128:
                raise ValidationError('Enter a schedule name of at most 128 characters.')
            files = data.get('files')
            config = validate_schedule(files, data.get('settings'))
            values = {'engine': engine, 'owner': owner, 'name': name, 'files': files, 'settings': config, 'updated': timezone.now()}
            if selected:
                changed = TrainingSchedule.objects.filter(pk=selected.pk, version=int(data.get('version', 0))).update(**values, version=F('version') + 1)
                if not changed:
                    return JsonResponse({'error': 'This schedule changed in another tab. Your edits are still here; reload or save a copy.'}, status=409)
                selected.refresh_from_db()
            else:
                selected = TrainingSchedule.objects.create(**values)
            return JsonResponse({'url': '/training/schedules/%s/' % selected.pk, 'version': selected.version})
        except (ValidationError, ValueError, TypeError, IntegrityError) as error:
            return JsonResponse({'error': 'A schedule with this name already exists.' if isinstance(error, IntegrityError) else error_text(error)}, status=400)
    rows = schedules_for(request.user)
    if request.method == 'GET' and not selected and not create:
        return render(request, 'training_catalog.html', {'page_title': 'New train', 'training_tab': 'schedules', 'schedules': rows.defer('files'), 'engines': EngineConfig.objects.filter(enabled=True).order_by('name')})
    payload = {
        'id': str(selected.pk) if selected else '',
        'version': selected.version if selected else 0,
        'name': selected.name if selected else '',
        'engine': str(selected.engine_id) if selected and selected.engine_id else request.GET.get('engine', ''),
        'scope': selected.scope if selected else 'personal',
        'files': selected.files if selected else {'examples/mattbench.rs': ''},
        'settings': {**DEFAULT_SETTINGS, **selected.settings} if selected else DEFAULT_SETTINGS,
        'editable': not selected or selected.owner_id == request.user.pk or request.user.is_superuser,
    }
    if request.GET.get('copy') and selected:
        payload.update(id='', version=0, name=selected.name + ' copy', scope='personal', editable=True)
    return render(request, 'training_schedules.html', {
        'page_title': selected.name if selected else 'New schedule', 'training_tab': 'schedules', 'schedules': rows, 'selected': selected,
        'schedule_data': payload, 'engines': EngineConfig.objects.filter(enabled=True).order_by('name'),
        'builder_available': selected and 'mattbench-builder.json' in selected.files,
    })


from OpenBench.schedule_builder import schedule_dataset_stages


class TrainingForm(forms.Form):
    checkpoint = forms.ModelChoiceField(label='Starting point', queryset=TrainingCheckpoint.objects.none(), required=False, empty_label='Start from scratch')
    wdl = forms.FloatField(label='WDL for remaining training', min_value=0, max_value=1, required=False, widget=forms.NumberInput(attrs={'step': '0.01', 'placeholder': 'Use schedule values'}))
    skip_broken_games = forms.BooleanField(label='Skip invalid games and report discarded data', initial=True, required=False)
    name = forms.RegexField(label='Network name', regex=r'\A[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z', max_length=64, error_messages={'invalid': 'Use letters, numbers, dots, dashes or underscores, starting with a letter or number.'})
    engine = forms.ModelChoiceField(queryset=EngineConfig.objects.filter(enabled=True).order_by('name'))
    dataset = forms.ModelChoiceField(queryset=TrainingDataset.objects.none(), widget=forms.HiddenInput, required=False)
    stage_datasets = forms.JSONField(required=False, widget=forms.HiddenInput)
    schedule = forms.ModelChoiceField(queryset=TrainingSchedule.objects.none())
    worker = forms.ModelChoiceField(queryset=TrainingWorker.objects.none(), required=False, empty_label='Any compatible worker accepting my workloads')
    environment = forms.CharField(label='Environment variables', required=False, strip=False, max_length=32768, widget=forms.Textarea(attrs={'rows': 3, 'placeholder': 'KEY=value'}))
    checkpoint_retention = forms.ChoiceField(choices=[('latest', 'Keep latest'), ('all', 'Keep all checkpoints')], initial='latest')
    checkpoint_keep_last = forms.IntegerField(label='Checkpoints to keep', min_value=1, max_value=10000, initial=DEFAULT_SETTINGS['checkpoint_keep_last'], required=False)
    delete_uploaded_checkpoints = forms.BooleanField(label='Delete local copies after upload', initial=DEFAULT_SETTINGS['delete_uploaded_checkpoints'], required=False)

    def __init__(self, user, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['checkpoint'].queryset = TrainingCheckpoint.objects.filter(run__owner=user, run__snapshot__settings__resume_supported=True).exclude(run__snapshot__has_key='demo').select_related('run', 'archive').order_by('-run_id', '-superbatch')
        self.fields['checkpoint'].label_from_instance = lambda row: '%s · Run #%d · SB %d → %d' % (row.run.name, row.run_id, row.superbatch, row.superbatch + 1)
        self.fields['schedule'].queryset = schedules_for(user).filter(Q(owner=user) | Q(engine__enabled=True) | Q(engine=None))
        self.fields['schedule'].label_from_instance = lambda row: '%s (%s)' % (row.name, row.scope_label)
        cutoff = timezone.now() - timedelta(minutes=2)
        self.fields['worker'].queryset = TrainingWorker.objects.filter(Q(updated__gte=cutoff) | Q(machine__updated__gte=cutoff), Q(owner=user) | Q(accept_any_owner=True), enabled=True).exclude(info__has_key='demo').select_related('machine')
        busy_workers = set(TrainingRun.objects.filter(state__in=TRAINING_ACTIVE).values_list('worker_id', flat=True))
        self.fields['worker'].label_from_instance = lambda row: '%s / %s (%s; %s)' % (row.name, row.info.get('gpu', 'GPU'), 'Offline' if max(row.updated, row.machine.updated if row.machine_id else row.updated) < cutoff else 'Busy' if row.pk in busy_workers or row.machine_id and row.machine.workload else 'Online', row.machine.get_mode_display() if row.machine_id else row.get_mode_display())
        for name, field in self.fields.items():
            field.widget.attrs.update({'id': 'train-' + name})
        self.fields['dataset'].queryset = TrainingDataset.objects.filter(archived=False, checked__isnull=False)
        self.fields['name'].widget.attrs['placeholder'] = 'Network name'

    def clean_environment(self):
        environment = {}
        for index, line in enumerate(self.cleaned_data['environment'].split('\n'), 1):
            line = line.removesuffix('\r')
            if not line.strip():
                continue
            key, separator, value = line.partition('=')
            if not separator or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key) or '\x00' in value or '\r' in value:
                raise ValidationError('Row %d must use KEY=value with a valid variable name.' % index)
            if key.upper().startswith('MATTBENCH_'):
                raise ValidationError('Row %d uses a reserved MATTBENCH_ variable name.' % index)
            if key in environment:
                raise ValidationError('Row %d repeats a variable name.' % index)
            environment[key] = value
        return environment

    def clean(self):
        data = super().clean()
        schedule = data.get('schedule')
        ranges = schedule_dataset_stages(schedule) if schedule else []
        overrides = data.get('stage_datasets') or []
        stage_count = max(1, len(ranges)) if schedule else 0
        selected = {}
        if not isinstance(overrides, list):
            self.add_error('stage_datasets', 'Invalid stage overrides.')
        else:
            available = {str(row.pk): row for row in self.fields['dataset'].queryset}
            for override in overrides:
                if not isinstance(override, dict) or type(override.get('stage')) is not int or not 0 <= override['stage'] < stage_count or override['stage'] in selected:
                    self.add_error('stage_datasets', 'Choose each stage once.')
                    break
                dataset = available.get(str(override.get('dataset', '')))
                if not dataset:
                    self.add_error('stage_datasets', 'Choose an available registered dataset for each stage.')
                    break
                selected[override['stage']] = dataset
        if schedule and len(selected) != stage_count:
            self.add_error('stage_datasets', 'Choose a dataset for every stage.')
        data['dataset'] = selected.get(0)
        data['dataset_stages'] = [{**selected[index].snapshot(), **bounds} for index, bounds in enumerate(ranges)] if len(selected) == stage_count else []
        config = {**DEFAULT_SETTINGS, **(schedule.settings if schedule else {})}
        if data.get('checkpoint_retention') == 'all':
            config['checkpoint_keep_last'] = 0
        elif data.get('checkpoint_retention') == 'latest':
            if data.get('checkpoint_keep_last') is None and 'checkpoint_keep_last' not in self.errors:
                self.add_error('checkpoint_keep_last', 'Enter how many checkpoints to keep.')
            elif data.get('checkpoint_keep_last') is not None:
                config['checkpoint_keep_last'] = data['checkpoint_keep_last']
        config['delete_uploaded_checkpoints'] = data.get('delete_uploaded_checkpoints', False)
        config['skip_broken_games'] = data.get('skip_broken_games', True)
        data['run_settings'] = config
        if schedule and 'mattbench-demo.json' in schedule.files:
            self.add_error('schedule', 'This is a demo schedule. Choose a runnable schedule to start training.')
        if schedule and schedule.scope == 'engine' and data.get('engine') and schedule.engine_id != data['engine'].pk:
            self.add_error('schedule', 'Choose a schedule for this engine.')
        worker = data.get('worker')
        if schedule and worker:
            for error in worker_requirement_errors(worker.info, config):
                self.add_error('worker', error)
        checkpoint = data.get('checkpoint')
        if data.get('wdl') is not None and not checkpoint:
            self.add_error('wdl', 'Select a checkpoint to change WDL for the remaining training.')
        if schedule:
            from OpenBench.schedule_builder import builder_state, generate_schedule
            spec, current = builder_state(schedule)
            files = schedule.files
            if checkpoint and data.get('wdl') is not None:
                if not current:
                    self.add_error('wdl', 'WDL overrides require a schedule managed by the schedule builder.')
                else:
                    for stage in spec['wdl_stages']:
                        if stage['end'] > checkpoint.superbatch:
                            stage.update(kind='constant', initial=data['wdl'], final=data['wdl'])
            if current:
                _, files, _ = generate_schedule(spec)
            if checkpoint and data.get('engine'):
                from OpenBench.training_checkpoints import validate_checkpoint_schedule
                try:
                    validate_checkpoint_schedule(checkpoint, data['engine'], files, config)
                except ValidationError as error:
                    self.add_error('checkpoint', error)
            data['run_files'] = files
        return data


@login_required(login_url='/login/')
@require_http_methods(['GET', 'POST'])
@transaction.atomic
def new_training(request):
    from OpenBench.views import redirect, render
    enabled(request.user)
    initial = {'dataset': request.GET.get('dataset', ''), 'schedule': request.GET.get('schedule'), 'engine': request.GET.get('engine')}
    if request.method == 'GET' and request.GET.get('checkpoint'):
        if not request.GET['checkpoint'].isdigit() or len(request.GET['checkpoint']) > 18:
            return redirect(request, '/training/new/', error='Choose a saved checkpoint.')
        selected_checkpoint = get_object_or_404(TrainingCheckpoint.objects.select_related('run').exclude(run__snapshot__has_key='demo'), pk=request.GET['checkpoint'], run__owner=request.user, run__snapshot__settings__resume_supported=True)
        source = selected_checkpoint.run
        source_datasets = source.dataset.get('stages') or [source.dataset]
        initial.update(checkpoint=selected_checkpoint.pk, engine=source.engine_id, schedule=source.schedule_id, name=('%s-sb%d' % (source.name[:45], selected_checkpoint.superbatch)),
                       dataset=source_datasets[0].get('registry_id', ''), stage_datasets=[{'stage': index, 'dataset': stage.get('registry_id', '')} for index, stage in enumerate(source_datasets)],
                       environment='\n'.join('%s=%s' % pair for pair in source.snapshot.get('environment', {}).items()))
    form = TrainingForm(request.user, request.POST if request.method == 'POST' else None, initial=initial)
    if request.method == 'POST' and form.is_valid():
        try:
            hf_token(request.user.pk)
            data = form.cleaned_data
            schedule = data['schedule']
            files = data.get('run_files', schedule.files)
            config = validate_schedule(files, data['run_settings'])
            checkpoint = data.get('checkpoint')
            snapshot = {'name': schedule.name, 'version': schedule.version, 'files': files, 'settings': config, 'environment': data['environment']}
            if checkpoint:
                from OpenBench.training_checkpoints import checkpoint_provenance
                from OpenBench.training_storage import storage_lock
                with storage_lock():
                    checkpoint = TrainingCheckpoint.objects.select_for_update().select_related('archive', 'run').filter(pk=checkpoint.pk).first()
                    if checkpoint is None:
                        raise ValidationError('This checkpoint was removed. Choose another starting point.')
                    snapshot['resume'] = checkpoint_provenance(checkpoint)
                    snapshot['bullet_commit'] = checkpoint.run.snapshot.get('bullet_commit')
                if data.get('wdl') is not None:
                    snapshot['wdl_override'] = data['wdl']
            with transaction.atomic():
                if data['worker']:
                    if data['worker'].machine_id:
                        Machine.objects.select_for_update().get(pk=data['worker'].machine_id)
                    data['worker'] = TrainingWorker.objects.select_for_update().get(pk=data['worker'].pk)
                    if not data['worker'].enabled:
                        raise ValidationError('This worker was disconnected. Select another worker.')
                    if data['worker'].owner_id != request.user.pk and not data['worker'].accept_any_owner:
                        raise ValidationError('This worker no longer accepts workloads from other accounts.')
                run = TrainingRun.objects.create(
                    owner=request.user, engine=data['engine'], name=data['name'], schedule=schedule,
                    requested_worker=data['worker'],
                    snapshot=snapshot, resume_from=checkpoint,
                    dataset=({'repo': data['dataset_stages'][0]['repo'], 'stages': data['dataset_stages']} if data['dataset_stages'] else data['dataset'].snapshot()),
                )
            record_event('training.created', run, request.user.pk, {'engine': run.engine.name, 'name': run.name, 'checkpoint_id': checkpoint.pk if checkpoint else None})
            return redirect(request, '/training/%d/' % run.pk)
        except ValidationError as error:
            form.add_error(None, error)
    options = [{'id': str(row.pk), 'engine': str(row.engine_id) if row.scope == 'engine' else '', 'name': row.name, 'scope': row.scope_label, 'stages': schedule_dataset_stages(row)} for row in schedules_for(request.user).filter(Q(owner=request.user) | Q(engine__enabled=True) | Q(engine=None))]
    dataset_options = [{'id': str(row.pk), 'name': row.name, 'repo': row.repo, 'revision': row.revision, 'owner': row.owner.username, 'is_owner': row.owner_id == request.user.pk} for row in form.fields['dataset'].queryset.select_related('owner')]
    dataset_options.sort(key=lambda row: (not row['is_owner'], row['name'].casefold(), row['owner'].casefold(), row['id']))
    checkpoint_options = [{'id': str(row.pk), 'engine': str(row.run.engine_id), 'schedule': str(row.run.schedule_id or ''), 'superbatch': row.superbatch, 'run': row.run_id, 'name': row.run.name, 'datasets': [stage.get('registry_id', '') for stage in (row.run.dataset.get('stages') or [row.run.dataset])]} for row in form.fields['checkpoint'].queryset]
    return render(request, 'training_new.html', {'page_title': 'New train', 'training_tab': 'new', 'form': form, 'schedule_options': options, 'dataset_options': dataset_options, 'checkpoint_options': checkpoint_options, 'hf_connected': HuggingFaceCredential.objects.filter(user=request.user).exists()})


@login_required(login_url='/login/')
@require_http_methods(['GET', 'POST'])
@transaction.atomic
def training_detail(request, pk):
    from OpenBench.views import redirect, render
    run = get_object_or_404(visible_runs(request.user), pk=pk)
    manage = may_manage(request.user, run)
    if request.method == 'POST':
        enabled(request.user)
        if not manage:
            raise PermissionDenied
        if run.snapshot.get('demo'):
            return redirect(request, '/training/%d/' % pk, error='Demo runs cannot be dispatched or modified.')
        action = request.POST.get('action')
        if action in ('delete', 'restore'):
            deleted = action == 'delete'
            TrainingRun.objects.filter(pk=pk).update(deleted=deleted, updated=timezone.now())
            record_event('training.deleted' if deleted else 'training.restored', run, request.user.pk)
            return redirect(request, '/training/', status='Workload was Deleted!' if deleted else 'Workload was Restored!')
        if run.deleted:
            return redirect(request, '/training/%d/' % pk, error='Restore this workload before modifying it.')
        if request.POST.get('action') == 'cancel':
            now = timezone.now()
            cancelled = TrainingRun.objects.filter(pk=pk, state__in=('VALIDATING', 'PREPARING', 'QUEUED')).update(state='CANCELLED', finished=now, updated=now, cancel_requested=True)
            TrainingRun.objects.filter(pk=pk, state__in=TRAINING_ACTIVE).update(cancel_requested=True)
            TrainingRun.objects.filter(pk=pk, state='FAILED', recovery_run=None, metrics__recovery_pending=1).update(cancel_requested=True, error='Automatic recovery cancelled.')
            record_event('training.cancel.requested', run, request.user.pk)
            if cancelled:
                record_event('training.cancelled', run, request.user.pk)
        elif request.POST.get('action') == 'resume':
            return redirect(request, '/training/new/', error='Select a checkpoint and settings in New train to create a separate task.')
        elif request.POST.get('action') == 'finish-checkpoints':
            from OpenBench.training_checkpoints import finish_checkpoints
            try:
                if not run.terminal:
                    raise ValidationError('Finish or stop training before cleaning up its checkpoints.')
                finish_checkpoints(run, request.user, request.POST.getlist('keep'))
                return redirect(request, '/training/%d/' % pk, status='Selected networks imported; remaining checkpoint files deleted.')
            except ValidationError as error:
                return redirect(request, '/training/%d/' % pk, error=error_text(error))
        elif request.POST.get('action') == 'restart' and run.terminal:
            return redirect(request, '/training/new/?engine=%s&schedule=%s' % (run.engine_id, run.schedule_id or ''))
        return redirect(request, '/training/%d/' % pk)
    from OpenBench.training_telemetry import training_metrics
    run.metrics = training_metrics(run.metrics, run.snapshot, run.dataset, run.state)
    output_artifacts = list(run.artifacts.select_related('checkpoint_network', 'checkpoint_archive'))
    for item in output_artifacts:
        checkpoint = getattr(item, 'checkpoint_network', None) or getattr(item, 'checkpoint_archive', None)
        match = re.fullmatch(r'sb-(\d+)\.bin', item.name)
        item.output_superbatch = checkpoint.superbatch if checkpoint else int(match[1]) if match else 0
        item.start_checkpoint_id = checkpoint.pk if checkpoint and item.kind == 'checkpoint' and run.owner_id == request.user.pk and run.snapshot['settings'].get('resume_supported') else None
        item.output_label = {'network': 'Network', 'checkpoint': 'Resume checkpoint', 'manifest': 'Manifest', 'log': 'Log'}.get(item.kind, item.kind)
        if item.kind == 'network' and item.output_superbatch:
            final = run.state == 'COMPLETED' and item.output_superbatch == run.metrics.get('end_superbatch', run.metrics.get('superbatch'))
            item.output_label = '%s · SB %d' % ('Final network' if final else 'Network', item.output_superbatch)
    output_artifacts.sort(key=lambda item: (item.kind != 'network', item.kind != 'checkpoint', -item.output_superbatch, item.name))
    return render(request, 'training_detail.html', {
        'page_title': run.name, 'training_tab': 'runs', 'run': run, 'can_manage': manage and not run.snapshot.get('demo'),
        'can_register': manage and not run.snapshot.get('demo'),
        'latest_checkpoint': run.checkpoints.first(),
        'output_artifacts': output_artifacts,
    })


@login_required(login_url='/login/')
def training_log(request, pk):
    run = get_object_or_404(visible_runs(request.user), pk=pk)
    path = Path(settings.TRAINING_ROOT) / str(run.pk) / 'worker.log'
    if not path.is_file():
        return JsonResponse({'error': 'No log has arrived yet.'}, status=404)
    return FileResponse(path.open('rb'), as_attachment=True, filename='training-%d.log' % pk)


@login_required(login_url='/login/')
def training_configuration(request, pk):
    run = get_object_or_404(visible_runs(request.user), pk=pk)
    response = JsonResponse({**run.snapshot, 'dataset': run.dataset, 'parameters': run.parameters}, json_dumps_params={'indent': 2})
    response['Content-Disposition'] = 'attachment; filename="training-%d.json"' % pk
    response['Cache-Control'] = 'private, no-store'
    return response


@login_required(login_url='/login/')
@require_http_methods(['GET', 'POST'])
def artifact(request, pk, artifact_id):
    from OpenBench.views import redirect
    run = get_object_or_404(visible_runs(request.user), pk=pk)
    row = get_object_or_404(TrainingArtifact.objects.select_related('run__engine', 'run__owner'), pk=artifact_id, run=run)
    path = Path(settings.TRAINING_ROOT) / row.path
    if request.method == 'GET':
        response = FileResponse(path.open('rb'), as_attachment=True, filename=row.name)
        response['Cache-Control'] = 'private, no-store'
        response['X-Checksum-SHA256'] = row.sha256
        response['Content-Length'] = str(row.size)
        return response
    enabled(request.user)
    if run.snapshot.get('demo') or not may_manage(request.user, row.run) or row.kind != 'network':
        raise PermissionDenied
    from django.core.files import File
    from django.core.files.storage import FileSystemStorage
    sha = row.sha256[:8].upper()
    storage = FileSystemStorage()
    try:
        with transaction.atomic():
            if row.network_id:
                return redirect(request, '/training/%d/' % pk, status='Network already registered')
            existing = Network.objects.filter(sha256=sha).first()
            if existing:
                raise ValidationError('This short network hash is already registered. Download the artifact to inspect it.')
            name = request.POST.get('name', row.run.name).strip()
            if not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', name) or Network.objects.filter(engine=row.run.engine.name, name=name).exists():
                raise ValidationError('Choose a unique network name of at most 64 characters using letters, numbers, dots, dashes or underscores.')
            destination = sha
            if storage.exists(destination):
                raise ValidationError('A network with this short hash already exists on disk.')
            with path.open('rb') as source:
                saved = storage.save(destination, File(source))
            if saved != destination:
                storage.delete(saved)
                raise ValidationError('Another network upload used this hash. Try again.')
            network = Network.objects.create(sha256=sha, name=name, engine=row.run.engine.name, author=request.user.username)
            TrainingArtifact.objects.filter(pk=row.pk).update(network=network)
            record_event('network.registered', network, request.user.pk, {'training_run_id': pk, 'artifact_id': row.pk})
    except ValidationError as error:
        return redirect(request, '/training/%d/' % pk, error=error_text(error))
    return redirect(request, '/training/%d/' % pk, status='Network registered')


@login_required(login_url='/login/')
@require_http_methods(['GET', 'POST'])
def dataset_upload(request, pk):
    from OpenBench.views import redirect, render
    from OpenBench.utils import getRecentMachines
    enabled(request.user)
    workload = get_object_or_404(Test, pk=pk, test_mode='DATAGEN', deleted=False)
    form_error = None
    if request.method == 'POST':
        try:
            if workload.execution.get('demo'):
                raise ValidationError('Demo datasets cannot be uploaded.')
            hf_token(request.user.pk)
            if not workload.finished or getRecentMachines().filter(workload=pk).exists() or PGN.objects.filter(test_id=pk, processed=False).exists():
                raise ValidationError('The archive is still being assembled. Wait until datagen and its workers have finished.')
            if not (Path(settings.MEDIA_ROOT) / 'PGNs' / ('%d.pgn.tar' % pk)).is_file():
                raise ValidationError('No PGN archive is available for this datagen.')
            repository = repo_id(request.POST.get('repo', ''))
            filename = relative_path(request.POST.get('filename', '').strip())
            if not filename.endswith('.pgn.tar'):
                raise ValidationError('Keep the .pgn.tar extension; the archive is uploaded unchanged.')
            DatasetUpload.objects.create(owner=request.user, workload=workload, repo=repository, filename=filename, private=request.POST.get('visibility') != 'public')
            return redirect(request, '/datagen/%d/huggingface/' % pk, status='Upload queued')
        except (ValidationError, IntegrityError) as error:
            form_error = 'An upload to that path is already queued.' if isinstance(error, IntegrityError) else error_text(error)
    connection = HuggingFaceCredential.objects.filter(user=request.user).first()
    return render(request, 'dataset_upload.html', {
        'page_title': 'Upload to Hugging Face', 'workload': workload,
        'uploads': DatasetUpload.objects.filter(workload=workload, owner=request.user)[:20],
        'destination': request.POST.get('repo', (connection.account + '/' if connection else '') + 'chess-training'),
        'filename': request.POST.get('filename', '%d.pgn.tar' % pk), 'form_error': form_error,
        'hf_connected': bool(connection),
    })


@login_required(login_url='/login/')
@require_http_methods(['GET', 'POST'])
@transaction.atomic
def workers(request):
    from OpenBench.views import redirect
    enabled(request.user)
    if request.method == 'POST':
        worker = get_object_or_404(TrainingWorker, pk=request.POST.get('worker'), owner=request.user)
        if worker.info.get('demo'):
            raise PermissionDenied
        TrainingWorker.objects.filter(pk=worker.pk).update(enabled=False)
        for run in TrainingRun.objects.filter(worker=worker, state__in=TRAINING_ACTIVE):
            changed = TrainingRun.objects.filter(pk=run.pk, state__in=TRAINING_ACTIVE).update(state='FAILED', error='Worker disconnected by its owner.', finished=timezone.now(), updated=timezone.now())
            if changed:
                record_event('training.interrupted', run, run.owner_id, {'reason': 'worker_disconnected', 'latest_checkpoint_id': run.checkpoints.values_list('pk', flat=True).first()})
        record_event('worker.disconnected', worker, request.user.pk)
    return redirect(request, '/workers/')


@login_required(login_url='/login/')
def lifecycle_events(request):
    enabled(request.user)
    try:
        after = max(0, int(request.GET.get('after', 0)))
    except ValueError:
        return JsonResponse({'error': 'Invalid event cursor.'}, status=400)
    rows = LifecycleEvent.objects.filter(id__gt=after)
    if not request.user.is_superuser:
        rows = rows.filter(owner=request.user)
    events = list(rows.values('id', 'event_id', 'kind', 'subject_type', 'subject_id', 'version', 'data', 'created')[:100])
    return JsonResponse({'events': events, 'next': events[-1]['id'] if events else after})


@login_required(login_url='/login/')
def checkpoint_list(request, pk):
    run = get_object_or_404(visible_runs(request.user), pk=pk)
    try:
        before = int(request.GET.get('before', 10000001))
    except ValueError:
        return JsonResponse({'error': 'Invalid checkpoint cursor.'}, status=400)
    rows = run.checkpoints.filter(superbatch__lt=before).select_related('archive', 'network')[:100]
    items = [{'id': row.pk, 'superbatch': row.superbatch, 'archive': row.archive_id, 'network': row.network_id, 'size': row.archive.size, 'sha256': row.archive.sha256, 'imported': bool(row.network.network_id)} for row in rows]
    return JsonResponse({'checkpoints': items, 'total': run.checkpoints.count(), 'latest': run.checkpoints.values_list('pk', flat=True).first(), 'next': items[-1]['superbatch'] if items else None})
