from django.core.exceptions import ValidationError
from django.utils import timezone


def validate_policy(value):
    if not isinstance(value, dict) or set(value) - {'cpu', 'threads', 'require', 'prefer', 'fallback_seconds', 'tags', 'max_workers'}:
        raise ValidationError('Invalid step worker settings.')
    policy = {'cpu': 'inherit', 'threads': 1, 'require': '', 'prefer': '', 'fallback_seconds': 300, 'tags': [], 'max_workers': 0, **value}
    if policy['cpu'] not in ('inherit', 'physical', 'logical', 'limit'):
        raise ValidationError('Choose inherited, physical-core, logical-core or fixed CPU allocation.')
    for key, maximum in (('threads', 4096), ('fallback_seconds', 86400), ('max_workers', 10000)):
        if type(policy[key]) is not int or not (1 if key == 'threads' else 0) <= policy[key] <= maximum:
            raise ValidationError('Invalid worker setting: ' + key)
    for key in ('require', 'prefer'):
        if not isinstance(policy[key], str) or len(policy[key]) > 64:
            raise ValidationError('Choose a valid worker identifier.')
    if not isinstance(policy['tags'], list) or len(policy['tags']) > 32 or any(not isinstance(tag, str) or not 1 <= len(tag) <= 32 for tag in policy['tags']):
        raise ValidationError('Worker tags must be short names.')
    return policy


def identity(machine=None, worker=None):
    identifiers = set()
    if worker:
        identifiers.add('gpu:' + str(worker.pk))
        if worker.machine_id:
            identifiers.add('cpu:' + str(worker.machine_id))
    if machine:
        identifiers.add('cpu:' + str(machine.pk))
        capability = getattr(machine, 'training_capability', None)
        if capability:
            identifiers.add('gpu:' + str(capability.pk))
    return identifiers


def allocation(info, policy, fallback):
    mode = policy.get('cpu', 'inherit')
    if mode == 'inherit':
        return {'threads': fallback, 'cpu_ids': []}
    topology = info.get('cpu_topology', {})
    if not isinstance(topology, dict):
        return None
    available = topology.get('logical', [])
    physical = topology.get('physical', [])
    if any(not isinstance(items, list) or len(items) > 4096 or any(type(cpu) is not int or not 0 <= cpu <= 65535 for cpu in items) or len(set(items)) != len(items) for items in (available, physical)):
        return None
    if not set(physical) <= set(available):
        return None
    if not available or mode == 'physical' and not physical:
        return None
    selected = physical if mode == 'physical' else available
    limit = info.get('worker_settings', {}).get('workflow_threads', info.get('workflow_threads', len(available)))
    if type(limit) is not int or not 1 <= limit <= 4096:
        return None
    requested = policy['threads'] if mode == 'limit' else len(selected)
    if requested > limit or requested > len(selected) or requested < 1:
        return None
    return {'threads': requested, 'cpu_ids': selected[:requested]}


def eligible(subject, *, machine=None, worker=None):
    from OpenBench.models import WorkflowRun
    snapshot = subject.execution if hasattr(subject, 'execution') else subject.snapshot
    reference = snapshot.get('workflow', {})
    policy = snapshot.get('worker_policy', {})
    if not reference:
        return True
    run = WorkflowRun.objects.filter(pk=reference['run']).first()
    if run is None or run.state not in ('RUNNING', 'PAUSED'):
        return False
    owner_id = machine.user_id if machine else worker.owner_id
    if run.owner_id != owner_id:
        return False
    info = machine.info if machine else worker.info
    if not info.get('workflow_protocol'):
        return False
    limit = policy.get('max_workers', 0)
    if machine and limit and machine.workload != subject.pk:
        from OpenBench.utils import getRecentMachines
        if getRecentMachines().filter(workload=subject.pk).exclude(pk=machine.pk).count() >= limit:
            return False
    identifiers = identity(machine, worker)
    if policy.get('require') and policy['require'] not in identifiers:
        return False
    preferred = policy.get('prefer')
    if preferred and preferred not in identifiers:
        step = run.steps.filter(key=reference['step']).first()
        if not step or (timezone.now() - (step.started or run.created)).total_seconds() < policy.get('fallback_seconds', 300):
            return False
    tags = info.get('worker_settings', {}).get('tags', info.get('tags', []))
    if not set(policy.get('tags', [])) <= set(tags):
        return False
    fallback = info.get('concurrency', info.get('threads', 1))
    return allocation(info, policy, fallback) is not None
