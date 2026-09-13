from django.core.exceptions import ValidationError

from OpenBench.config import OPENBENCH_CONFIG, workload_execution
from OpenBench.models import TrainingRun, TrainingWorker
from OpenBench.training_models import TRAINING_ACTIVE
from OpenBench.workloads.get_workload import filter_valid_workloads, machine_info_list, valid_hardware_assignment


def training_reasons(capability, preferences, run):
    from OpenBench.dataset_manifest import required_disk_bytes
    from OpenBench.training import worker_requirement_errors
    reasons = []
    info = capability.info
    if not capability.enabled:
        reasons.append('Training capability is disabled. Register the client again.')
    if info.get('protocol') not in (3, 4):
        reasons.append('Training protocol is unsupported. Update and register the client.')
    if run.workload_size and (info.get('protocol', 0) < 4 or 'training-workloads' not in info.get('capabilities', [])):
        reasons.append('This run requires a client with training-workload support (protocol 4).')
    if not capability.accept_any_owner and run.owner_id != capability.owner_id:
        reasons.append('Worker accepts training from its own account only; this run belongs to another account.')
    if run.requested_worker_id and run.requested_worker_id != capability.pk:
        reasons.append('This run is restricted to another worker.')
    only = machine_info_list(preferences, 'only')
    if only and run.engine.name not in only:
        reasons.append('--only allows %s, but this run uses %s.' % (', '.join(only), run.engine.name))
    config = run.snapshot['settings']
    if config.get('execution_image') and config['execution_image'] != info.get('execution_image'):
        reasons.append('The worker’s execution image does not match the schedule’s required image.')
    required = required_disk_bytes(run.dataset, config) / 1024 ** 3 + config.get('disk_reserve_gb', 10)
    reasons.extend(worker_requirement_errors(info, config, required))
    return reasons


def training_options(capability, preferences):
    runs = TrainingRun.objects.filter(state='QUEUED', worker=None, cancel_requested=False, deleted=False).exclude(snapshot__has_key='demo').select_related('engine')
    return [run for run in runs if not training_reasons(capability, preferences, run)]


def diagnose_training(request, machine, worker, run):
    from OpenBench.training_workloads import refine_candidates
    reasons, notes = [], []
    capability = TrainingWorker.objects.filter(machine=machine).first() if machine else TrainingWorker.objects.filter(pk=worker['id']).first()
    if not worker['online']:
        reasons.append('Worker is offline or disconnected. Start the client and wait for a fresh check-in.')
    if worker['mode'] not in ('automatic', 'training-only'):
        reasons.append('Worker is %s. Choose Automatic or Training only to accept training.' % worker['mode_label'])
    if not capability:
        reasons.append('Worker has not registered training capability. Enable training support in the client.')
        return {'eligible': False, 'reasons': reasons, 'notes': notes}
    preferences = machine.info if machine else capability.info
    reasons.extend(training_reasons(capability, preferences, run))
    if run.deleted or run.cancel_requested:
        reasons.append('This run was deleted or has cancellation requested.')
    if run.snapshot.get('demo'):
        reasons.append('Demo runs are excluded from real worker assignment.')
    if run.worker_id == capability.pk and run.state in TRAINING_ACTIVE:
        notes.append('This run is already assigned to this worker; it does not need another claim.')
    elif run.worker_id:
        reasons.append('This run is currently assigned to another worker.')
    elif run.state != 'QUEUED':
        reasons.append('Run is %s, not queued for assignment.' % run.state)
    if machine and machine.workload:
        reasons.append('Worker still has an engine test assignment. It must report its current batch complete first.')
    if TrainingRun.objects.filter(worker=capability, state__in=TRAINING_ACTIVE).exclude(pk=run.pk).exists():
        reasons.append('Worker already has another active training assignment.')
    if not reasons and run.state == 'QUEUED':
        eligible = training_options(capability, preferences)
        tests = filter_valid_workloads(request, machine, refine=False)[0] if machine and machine.mode == 'automatic' else []
        candidates, tests, _ = refine_candidates(eligible, tests, preferences)
        if run.pk not in {item.pk for item in candidates}:
            reasons.append('Other eligible workloads take precedence under --force, priority, or --focus. Force: %s; focus: %s; this run’s priority: %s.' % (', '.join(machine_info_list(preferences, 'force')) or 'none', ', '.join(machine_info_list(preferences, 'focus')) or 'none', run.priority))
        elif tests and capability.last_allocation_kind == 'training':
            reasons.append('Automatic mode will assign an eligible engine test next to alternate testing and training.')
    notes.append('Storage uses the latest client-reported available space, which can include reclaimable cache, and the same dataset estimate and reserve as the scheduler.')
    notes.append('Reported GPU memory: %s GiB. The current training scheduler checks GPU backend and storage; it does not enforce a VRAM minimum.' % capability.info.get('vram_gb', 'unknown'))
    notes.append('Eligibility is a snapshot. Queue order, other workers, and the next client poll can affect when the run starts.')
    return {'eligible': not reasons, 'reasons': reasons, 'notes': notes}


def diagnose_test(request, machine, worker, test):
    from OpenBench.utils import workload_uses_time_based_tc
    reasons = []
    notes = []
    if not worker['online']:
        reasons.append('Worker is disconnected or offline. Start the client and wait for a fresh check-in.')
    if worker['mode'] == 'paused':
        reasons.append('Worker is paused. Set its mode to Automatic or Testing only.')
    elif worker['mode'] == 'training-only':
        reasons.append('Worker is in Training only mode and does not accept tests.')
    if not test.approved:
        reasons.append('This test is awaiting approval.')
    if test.finished or test.deleted:
        reasons.append('This test is no longer active.')
    if test.execution.get('demo'):
        reasons.append('Demo workloads are excluded from real worker assignment.')
    if machine is None:
        reasons.append('This is a standalone training worker; it cannot run engine tests.')
        return {'reasons': reasons, 'notes': notes, 'eligible': False}
    info = machine.info
    for engine in dict.fromkeys((test.dev_engine, test.base_engine)):
        config = OPENBENCH_CONFIG['engines'].get(engine)
        if not config:
            reasons.append('%s is disabled or unavailable on this server.' % engine)
            continue
        if engine not in info.get('supported', []):
            details = []
            build = config['build']
            missing = [flag for flag in build['cpuflags'] if flag not in info.get('cpu_flags', [])]
            if missing:
                details.append('missing CPU instructions: %s' % ', '.join(missing))
            if info.get('os_name') not in build['systems']:
                details.append('operating system %s is not supported' % info.get('os_name', 'unknown'))
            if config['private'] and engine not in info.get('tokens', {}):
                details.append('client has not reported credentials for this private engine')
            if not config['private'] and engine not in info.get('compilers', {}):
                details.append('no compatible compiler reported; requires %s' % ', '.join(build['compilers']))
            reasons.append('%s is not in this worker’s supported engines: %s.' % (engine, '; '.join(details) or 'restart the client to refresh capabilities after engine configuration changes'))
    only = machine_info_list(info, 'only')
    if only and test.dev_engine not in only:
        reasons.append('--only allows %s, but this test uses %s.' % (', '.join(only), test.dev_engine))
    if info.get('noisy') and workload_uses_time_based_tc(test):
        reasons.append('--noisy excludes this test because it uses or measures time.')
    for value, label in ((test.syzygy_adj, 'adjudication'), (test.syzygy_wdl, 'WDL probing')):
        if value.endswith('-MAN') and int(value.split('-')[0]) > info.get('syzygy_max', 0):
            reasons.append('Syzygy %s needs %s tables; the worker reports %s-man support.' % (label, value, info.get('syzygy_max', 0)))
    try:
        workload_execution(test.book_name, [test.dev_engine, test.base_engine], test.execution.get('variant'))
    except ValidationError as error:
        reasons.extend(error.messages)
    try:
        if not valid_hardware_assignment(test, machine):
            reasons.append('Not enough assigned CPU threads (%s). Test options: dev [%s], base [%s]. SPSA needs two games at once; unequal thread counts may reduce usable hyperthreads.' % (info.get('concurrency', 0), test.dev_options, test.base_options))
    except (KeyError, TypeError, ValueError):
        reasons.append('CPU thread requirements could not be checked because worker or test settings are incomplete.')
    if TrainingRun.objects.filter(worker__machine=machine, state__in=TRAINING_ACTIVE).exists():
        reasons.append('Worker currently has an active training assignment. It must finish or release it before accepting a test.')
    if not reasons:
        try:
            options, _ = filter_valid_workloads(request, machine, refine=False)
            candidates, _ = filter_valid_workloads(request, machine)
            capability = TrainingWorker.objects.filter(machine=machine, enabled=True).first()
            combined = bool(capability and machine.mode == 'automatic' and capability.info.get('protocol') in (3, 4))
            if combined:
                from OpenBench.training_workloads import refine_candidates
                training, candidates, _ = refine_candidates(training_options(capability, info), options, info)
            if test.pk not in {item.pk for item in options}:
                reasons.append('The current scheduler excludes this test. Refresh the page and the client’s capabilities.')
            elif test.pk not in {item.pk for item in candidates}:
                if combined:
                    reasons.append('Other eligible tests or training runs take precedence under --force, priority, or --focus. This test’s priority is %s.' % test.priority)
                    return {'eligible': False, 'reasons': reasons, 'notes': ['The unified scheduler compares eligible tests and training runs together.']}
                forced = machine_info_list(info, 'force')
                if forced and any(item.dev_engine in forced for item in options) and test.dev_engine not in forced:
                    reasons.append('--force currently prefers an available test for %s.' % ', '.join(forced))
                else:
                    pool = [item for item in options if item.dev_engine in forced] if forced and any(item.dev_engine in forced for item in options) else options
                    priority = max(item.priority for item in pool)
                    if test.priority < priority:
                        reasons.append('Other eligible tests have higher priority (%s versus %s).' % (priority, test.priority))
                    else:
                        reasons.append('--focus currently prefers another engine among the highest-priority tests: %s.' % ', '.join(machine_info_list(info, 'focus')))
        except (KeyError, TypeError, ValueError):
            reasons.append('The scheduler could not evaluate incomplete worker settings. Restart the client to register its capabilities.')
    if machine.workload == test.pk:
        notes.append('This test is already assigned to the worker.')
    elif machine.workload:
        notes.append('Worker is assigned test #%s; it requests more work after its current batch.' % machine.workload)
    notes.append('Engine tests have no server-side free-storage threshold. Disk exhaustion, build failures, and the client’s temporary blacklist must be checked in its log; they are not reported with workload requests to this page.')
    notes.append('Eligibility does not guarantee immediate assignment: the scheduler also balances throughput across workers and tests.')
    return {'reasons': reasons, 'notes': notes, 'eligible': not reasons}
