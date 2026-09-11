import secrets
from datetime import timedelta

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from OpenBench.models import Machine, Test, TrainingRun, TrainingWorker
from OpenBench.training_models import TRAINING_ACTIVE


WORKER_MODES = [('automatic', 'Automatic'), ('training-only', 'Training only'), ('paused', 'Paused')]


def training_telemetry(job):
    if not job:
        return []
    fields = [
        ('gpu_percent', 'GPU use', '%'), ('gpu_used_mb', 'GPU memory', ' MB'),
        ('cpu_percent', 'CPU use', '%'), ('ram_used_gb', 'RAM used', ' GB'),
        ('disk_free_gb', 'Storage free', ' GB'),
        ('checkpoints_saved', 'Checkpoints stored', ''),
        ('last_checkpoint_superbatch', 'Latest checkpoint SB', ''),
        ('dataset_games', 'Dataset games', ''),
        ('preparation_threads', 'Preparation threads', ''),
        ('skipped_games', 'Invalid games skipped', ''),
        ('skipped_bytes', 'Invalid bytes skipped', ''),
    ]
    return [(label, '%s%s' % (format(job.metrics[key], ',.6g') if isinstance(job.metrics[key], (int, float)) else job.metrics[key], unit)) for key, label, unit in fields if key in job.metrics]

def worker_rows(user, identifier=None):
    now = timezone.now()
    cutoff = now - timedelta(minutes=2)
    machines = Machine.objects.select_related('user').order_by('-pk')
    if identifier is not None:
        machines = machines.filter(pk=identifier) if str(identifier).isdigit() else machines.none()
    machines = list(machines[:250])
    capabilities = {worker.machine_id: worker for worker in TrainingWorker.objects.filter(machine__in=machines, enabled=True)}
    active_training = {run.worker_id: run for run in TrainingRun.objects.filter(worker__in=capabilities.values(), state__in=TRAINING_ACTIVE).only('id', 'name', 'worker_id', 'metrics')}
    jobs = {row.pk: row for row in Test.objects.filter(pk__in=[machine.workload for machine in machines]).select_related('dev')}
    rows = []
    for machine in machines:
        info = machine.info
        job = jobs.get(machine.workload)
        online = not info.get('disconnected') and (machine.updated >= cutoff or bool(info.get('demo_online')))
        rows.append({
            'id': str(machine.pk), 'url': '/workers/%s/' % machine.pk,
            'mode': machine.mode, 'mode_label': machine.get_mode_display(), 'online': online,
            'manage': not info.get('disconnected') and not info.get('demo') and (user.pk == machine.user_id or user.is_superuser),
            'disconnect': not info.get('disconnected') and not info.get('demo') and (user.pk == machine.user_id or user.is_superuser),
            'current_workload': 'test:%s' % job.pk if job and not job.finished else '',
            'activity': 'Datagen' if job and not job.finished and job.test_mode == 'DATAGEN' else 'Testing' if job and not job.finished else 'Idle',
            'name': info.get('machine_name') or 'Worker %s' % machine.pk,
            'owner': machine.user, 'hardware': info.get('cpu_name', 'CPU'),
            'platform': info.get('os_name', ''), 'capabilities': ['Tests', 'Datagen', 'Tuning'],
            'threads': info.get('concurrency', 0), 'memory': round(info.get('ram_total_mb', 0) / 1024),
            'gpu_memory': None, 'updated': machine.updated, 'demo': bool(info.get('demo')),
            'state': 'Disconnected' if info.get('disconnected') else 'Busy' if online and job and not job.finished else 'Available' if online else 'Offline',
            'job': job.dev.name if online and job and not job.finished else '',
            'job_url': '/%s/%s/' % (job.workload_type_str(), job.pk) if job else '',
            'rate': '%s MNPS' % round(machine.dev_mnps + machine.base_mnps, 1) if machine.mnps else '',
            'details': [('Operating system', info.get('os_name', '')), ('CPU', info.get('cpu_name', '')), ('Instruction set', info.get('isa_name', '')), ('Logical cores', info.get('logical_cores', '')), ('Python', info.get('python_ver', '')), ('Client', info.get('client_ver', ''))],
        })
        capability = capabilities.get(machine.pk)
        if capability:
            row = rows[-1]
            row['capabilities'].extend(['Training', 'Data preparation'])
            row['gpu_memory'] = capability.info.get('vram_gb')
            row['details'].append(('GPU', capability.info.get('gpu', '')))
            training_job = active_training.get(capability.pk)
            if training_job:
                row.update(state='Busy' if online else 'Offline', activity='Training', job='Training', job_url='', rate='', current_workload='training:%s' % training_job.pk)
            if training_job and (user.pk == machine.user_id or user.is_superuser):
                row['telemetry'] = training_telemetry(training_job)
                row.update(state='Busy' if online else 'Offline', job=training_job.name, job_url='/training/%s/' % training_job.pk, rate='%s M pos/s' % round(training_job.metrics.get('positions_per_second', 0) / 1000000, 2))
    gpu_workers = TrainingWorker.objects.filter(machine__isnull=True).select_related('owner').order_by('pk')
    if identifier is not None:
        gpu_workers = gpu_workers.none() if str(identifier).isdigit() else gpu_workers.filter(pk=identifier)
    if not user.is_authenticated:
        gpu_workers = gpu_workers.none()
    elif not user.is_superuser:
        gpu_workers = gpu_workers.filter(owner=user)
    gpu_workers = list(gpu_workers[:250])
    training = {run.worker_id: run for run in TrainingRun.objects.filter(worker__in=gpu_workers, state__in=TRAINING_ACTIVE).only('id', 'name', 'worker_id', 'metrics')}
    for worker in gpu_workers:
        info = worker.info
        job = training.get(worker.pk)
        online = worker.enabled and (worker.updated >= cutoff or bool(info.get('demo_online')))
        rows.append({
            'id': str(worker.pk), 'url': '/workers/%s/' % worker.pk, 'name': worker.name,
            'mode': worker.mode, 'mode_label': worker.get_mode_display(), 'online': online,
            'manage': worker.enabled and not info.get('demo') and (user.pk == worker.owner_id or user.is_superuser),
            'current_workload': 'training:%s' % job.pk if job else '',
            'activity': 'Training' if job else 'Idle',
            'owner': worker.owner, 'hardware': info.get('gpu', 'GPU'),
            'platform': info.get('backend', '').upper(), 'capabilities': ['Training', 'Data preparation'],
            'threads': info.get('threads', 0), 'memory': info.get('ram_gb'),
            'gpu_memory': info.get('vram_gb'), 'updated': worker.updated, 'demo': bool(info.get('demo')),
            'state': 'Disconnected' if not worker.enabled else 'Busy' if online and job else 'Available' if online else 'Offline',
            'job': job.name if online and job else '', 'job_url': '/training/%s/' % job.pk if job else '',
            'rate': '%s M pos/s' % round(job.metrics.get('positions_per_second', 0) / 1000000, 2) if job else '',
            'telemetry': training_telemetry(job),
            'disconnect': worker.enabled and not info.get('demo') and (user.pk == worker.owner_id or user.is_superuser),
            'details': [('Backend', info.get('backend', '').upper()), ('GPU', info.get('gpu', '')), ('Free storage', '%s GB' % round(info.get('disk_gb', 0))), ('Device', info.get('device', '')), ('Protocol', info.get('protocol', ''))],
        })
    reservations = TrainingRun.objects.filter(requested_worker__isnull=False, state__in=('VALIDATING', 'PREPARING', 'QUEUED'), cancel_requested=False).exclude(snapshot__has_key='demo').select_related('requested_worker').order_by('created')
    reserved = {}
    for run in reservations:
        identifier = str(run.requested_worker.machine_id or run.requested_worker_id)
        reserved.setdefault(identifier, run)
    for row in rows:
        row['modes'] = WORKER_MODES
        if row['mode'] == 'paused' and row['online'] and row['activity'] == 'Idle':
            row['state'] = 'Paused'
        if row['manage'] and row['id'] in reserved:
            row['reservation'] = reserved[row['id']]
    return sorted(rows, key=lambda row: (row['name'].lower(), row['id']))


@require_http_methods(['GET'])
def index(request):
    from OpenBench.views import render
    rows = worker_rows(request.user)
    connected = [row for row in rows if row['online']]
    old = [row for row in rows if not row['online']]
    return render(request, 'workers.html', {
        'page_title': 'Workers',
        'worker_groups': [
            {'id': 'connected', 'title': 'Connected workers', 'workers': connected, 'empty': 'No workers connected'},
            {'id': 'old', 'title': 'Old workers', 'workers': old, 'empty': 'No old workers'},
        ],
        'online_count': len(connected),
        'busy_count': sum(row['state'] == 'Busy' for row in rows),
        'thread_count': sum(row['threads'] for row in connected if row['activity'] in ('Testing', 'Datagen') or row['activity'] == 'Idle' and row['mode'] == 'automatic' and 'Tests' in row['capabilities'] and not row.get('reservation')),
        'activity_counts': [(label, sum(row['activity'] == label for row in connected)) for label in ('Testing', 'Datagen', 'Training', 'Idle')],
    })


@require_http_methods(['GET', 'POST'])
def detail(request, pk):
    from OpenBench.views import render, redirect
    if not str(pk).isdigit():
        linked = TrainingWorker.objects.filter(pk=pk, machine__isnull=False).first()
        if linked:
            return redirect(request, '/workers/%s/' % linked.machine_id)
    rows = worker_rows(request.user, pk)
    worker = next((row for row in rows if row['id'] == str(pk)), None)
    if worker is None:
        from django.http import Http404
        raise Http404
    if request.method == 'POST':
        action = request.POST.get('action')
        if not worker.get('manage') or action not in ('settings', 'mode', 'stop', 'disconnect'):
            raise PermissionDenied
        from OpenBench.lifecycle import record_event
        from OpenBench.training_views import enabled
        enabled(request.user)
        with transaction.atomic():
            machine = Machine.objects.select_for_update().get(pk=pk) if str(pk).isdigit() else None
            capability = TrainingWorker.objects.select_for_update().filter(machine=machine).first() if machine else get_object_or_404(TrainingWorker.objects.select_for_update(), pk=pk)
            if action == 'settings':
                name = request.POST.get('name', '').strip()
                mode = request.POST.get('mode')
                if not 1 <= len(name) <= 128:
                    return redirect(request, worker['url'], error='Worker name must contain 1 to 128 characters.')
                if mode not in dict(WORKER_MODES):
                    return redirect(request, worker['url'], error='Choose a valid worker mode.')
                if machine:
                    info = {**machine.info, 'machine_name': name, 'custom_name': name}
                    Machine.objects.filter(pk=machine.pk).update(info=info, mode=mode)
                if capability:
                    info = {**capability.info, 'custom_name': name}
                    TrainingWorker.objects.filter(pk=capability.pk).update(name=name, info=info, mode=mode)
                return redirect(request, worker['url'], status='Worker settings saved.')
            if action in ('mode', 'stop'):
                mode = request.POST.get('mode') if action == 'mode' else 'paused'
                if mode not in dict(WORKER_MODES):
                    return redirect(request, worker['url'], error='Choose a valid worker mode.')
                if action == 'stop':
                    current = request.POST.get('workload', '')
                    run = TrainingRun.objects.filter(worker=capability, state__in=TRAINING_ACTIVE).first() if capability else None
                    expected = 'training:%s' % run.pk if run else 'test:%s' % machine.workload if machine and machine.workload else ''
                    if not expected or current != expected:
                        return redirect(request, worker['url'], error='The workload changed. Refresh and try again.')
                    if run:
                        TrainingRun.objects.filter(pk=run.pk, state__in=TRAINING_ACTIVE).update(cancel_requested=True)
                        record_event('training.cancel.requested', run, request.user.pk)
                    elif machine:
                        Machine.objects.filter(pk=machine.pk).update(workload=0, mnps=0, dev_mnps=0, base_mnps=0)
                if machine:
                    Machine.objects.filter(pk=machine.pk).update(mode=mode)
                if capability:
                    TrainingWorker.objects.filter(pk=capability.pk).update(mode=mode)
                return redirect(request, worker['url'], status='Stop requested; worker paused.' if action == 'stop' else 'Worker mode saved. Current work will finish before the mode takes effect.')
            if not worker.get('disconnect'):
                raise PermissionDenied
            if machine:
                info = {**machine.info, 'disconnected': True}
                Machine.objects.filter(pk=machine.pk).update(info=info, secret=secrets.token_hex(32), mode='paused', workload=0, mnps=0, dev_mnps=0, base_mnps=0)
            if capability:
                TrainingWorker.objects.filter(pk=capability.pk).update(enabled=False, mode='paused')
            for run in TrainingRun.objects.filter(worker=capability, state__in=TRAINING_ACTIVE) if capability else []:
                if TrainingRun.objects.filter(pk=run.pk, state__in=TRAINING_ACTIVE).update(state='FAILED', error='Worker disconnected by its owner.', finished=timezone.now(), updated=timezone.now()):
                    record_event('training.interrupted', run, run.owner_id, {'reason': 'worker_disconnected'})
            record_event('worker.disconnected', machine or capability, request.user.pk)
        return redirect(request, worker['url'])
    return render(request, 'worker_detail.html', {'page_title': worker['name'], 'worker': worker})
