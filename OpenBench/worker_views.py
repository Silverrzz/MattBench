from datetime import timedelta

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from OpenBench.models import Machine, Test, TrainingRun, TrainingWorker
from OpenBench.training_models import TRAINING_ACTIVE


def worker_rows(user, identifier=None):
    now = timezone.now()
    cutoff = now - timedelta(minutes=2)
    machines = Machine.objects.select_related('user').order_by('-pk')
    if identifier is not None:
        machines = machines.filter(pk=identifier) if str(identifier).isdigit() else machines.none()
    machines = list(machines[:250])
    jobs = {row.pk: row for row in Test.objects.filter(pk__in=[machine.workload for machine in machines]).select_related('dev')}
    rows = []
    for machine in machines:
        info = machine.info
        job = jobs.get(machine.workload)
        online = machine.updated >= cutoff or bool(info.get('demo_online'))
        rows.append({
            'id': str(machine.pk), 'url': '/workers/%s/' % machine.pk,
            'name': info.get('machine_name') or 'Worker %s' % machine.pk,
            'owner': machine.user, 'hardware': info.get('cpu_name', 'CPU'),
            'platform': info.get('os_name', ''), 'capabilities': ['Tests', 'Datagen', 'Tuning'],
            'threads': info.get('concurrency', 0), 'memory': round(info.get('ram_total_mb', 0) / 1024),
            'gpu_memory': None, 'updated': machine.updated, 'demo': bool(info.get('demo')),
            'state': 'Busy' if online and job and not job.finished else 'Available' if online else 'Offline',
            'job': job.dev.name if online and job and not job.finished else '',
            'job_url': '/%s/%s/' % (job.workload_type_str(), job.pk) if job else '',
            'rate': '%s MNPS' % round(machine.dev_mnps + machine.base_mnps, 1) if machine.mnps else '',
            'details': [('Operating system', info.get('os_name', '')), ('CPU', info.get('cpu_name', '')), ('Instruction set', info.get('isa_name', '')), ('Logical cores', info.get('logical_cores', '')), ('Python', info.get('python_ver', '')), ('Client', info.get('client_ver', ''))],
        })
    gpu_workers = TrainingWorker.objects.select_related('owner').order_by('pk')
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
            'owner': worker.owner, 'hardware': info.get('gpu', 'GPU'),
            'platform': info.get('backend', '').upper(), 'capabilities': ['Training', 'Data preparation'],
            'threads': info.get('threads', 0), 'memory': info.get('ram_gb'),
            'gpu_memory': info.get('vram_gb'), 'updated': worker.updated, 'demo': bool(info.get('demo')),
            'state': 'Disconnected' if not worker.enabled else 'Busy' if online and job else 'Available' if online else 'Offline',
            'job': job.name if online and job else '', 'job_url': '/training/%s/' % job.pk if job else '',
            'rate': '%s M pos/s' % round(job.metrics.get('positions_per_second', 0) / 1000000, 2) if job else '',
            'disconnect': worker.enabled and not info.get('demo') and (user.pk == worker.owner_id or user.is_superuser),
            'details': [('Backend', info.get('backend', '').upper()), ('GPU', info.get('gpu', '')), ('Free storage', '%s GB' % round(info.get('disk_gb', 0))), ('Device', info.get('device', '')), ('Protocol', info.get('protocol', ''))],
        })
    return sorted(rows, key=lambda row: (row['name'].lower(), row['id']))


@require_http_methods(['GET'])
def index(request):
    from OpenBench.views import render
    rows = worker_rows(request.user)
    return render(request, 'workers.html', {
        'page_title': 'Workers', 'workers': rows,
        'online_count': sum(row['state'] in ('Busy', 'Available') for row in rows),
        'busy_count': sum(row['state'] == 'Busy' for row in rows),
        'thread_count': sum(row['threads'] for row in rows if row['state'] in ('Busy', 'Available')),
    })


@require_http_methods(['GET', 'POST'])
def detail(request, pk):
    from OpenBench.views import render, redirect
    rows = worker_rows(request.user, pk)
    worker = next((row for row in rows if row['id'] == str(pk)), None)
    if worker is None:
        from django.http import Http404
        raise Http404
    if request.method == 'POST':
        if not worker.get('disconnect') or request.POST.get('action') != 'disconnect':
            raise PermissionDenied
        from OpenBench.lifecycle import record_event
        from OpenBench.training_views import enabled
        enabled(request.user)
        with transaction.atomic():
            row = get_object_or_404(TrainingWorker, pk=pk)
            TrainingWorker.objects.filter(pk=pk).update(enabled=False)
            for run in TrainingRun.objects.filter(worker=row, state__in=TRAINING_ACTIVE):
                if TrainingRun.objects.filter(pk=run.pk, state__in=TRAINING_ACTIVE).update(state='FAILED', error='Worker disconnected by its owner.', finished=timezone.now(), updated=timezone.now()):
                    record_event('training.interrupted', run, run.owner_id, {'reason': 'worker_disconnected'})
            record_event('worker.disconnected', row, request.user.pk)
        return redirect(request, worker['url'])
    return render(request, 'worker_detail.html', {'page_title': worker['name'], 'worker': worker})
