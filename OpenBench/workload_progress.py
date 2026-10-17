import math
import time


def pending_games(workload):
    state = workload.execution.get('pending_progress', {})
    if state.get('execution') != workload.execution_number:
        return 0
    now = time.time()
    return sum(value[1] for value in state.get('workers', {}).values()
               if isinstance(value, list) and len(value) == 2 and type(value[0]) in (int, float)
               and now - 120 <= value[0] <= now and type(value[1]) is int and 0 <= value[1] <= 1000000000)


def record_pending(workload, machine_id, games):
    now = time.time()
    state = workload.execution.get('pending_progress', {})
    workers = state.get('workers', {}) if state.get('execution') == workload.execution_number else {}
    workers = {key: value for key, value in workers.items() if isinstance(value, list) and len(value) == 2
               and type(value[0]) in (int, float) and now - 120 <= value[0] <= now}
    workers[str(machine_id)] = [now, games]
    workload.execution = {**workload.execution, 'pending_progress': {'execution': workload.execution_number, 'workers': workers}}
    record_progress(workload, workload.games, force=True)


def clear_pending(workload, machine_id):
    state = workload.execution.get('pending_progress', {})
    if state.get('execution') == workload.execution_number:
        workers = dict(state.get('workers', {}))
        workers.pop(str(machine_id), None)
        workload.execution = {**workload.execution, 'pending_progress': {**state, 'workers': workers}}


def record_progress(workload, previous_games, force=False):
    if not force and workload.games <= previous_games:
        return
    now = time.time()
    count = workload.games + pending_games(workload)
    state = workload.execution.get('progress', {})
    points = state.get('points', []) if state.get('execution') == workload.execution_number else []
    points = [point for point in points if isinstance(point, list) and len(point) == 2
              and all(type(value) in (int, float) and math.isfinite(value) for value in point)
              and now - 900 <= point[0] <= now and 0 <= point[1] <= count]
    if not points or now - points[-1][0] > 300:
        points = [[now, count]]
    if len(points) < 2 or points[-1][0] - points[-2][0] >= 5:
        points.append([now, count])
    else:
        points[-1] = [now, count]
    workload.execution = {**workload.execution, 'progress': {'execution': workload.execution_number, 'points': points[-182:]}}


def completion(workload):
    total = 2 * workload.spsa_run.pairs_per * workload.spsa_run.iterations if workload.test_mode == 'SPSA' else workload.max_games
    if workload.test_mode not in ('DATAGEN', 'SPSA', 'GAMES') or total <= 0:
        return None
    count = workload.games + (pending_games(workload) if not workload.finished else 0)
    percent = min(100, max(0, 100 * count / total))
    if workload.games >= total:
        return percent, 'Complete'
    if workload.finished or workload.deleted or not workload.approved:
        return percent, 'Stopped' if workload.finished or workload.deleted else 'Awaiting approval'
    if count >= total:
        return 99.9, 'Waiting for final results'
    state = workload.execution.get('progress', {})
    points = state.get('points', []) if state.get('execution') == workload.execution_number else []
    now = time.time()
    points = [p for p in points if isinstance(p, list) and len(p) == 2
              and all(type(v) in (int, float) and math.isfinite(v) for v in p)
              and now - 300 <= p[0] <= now and 0 <= p[1] <= count]
    if len(points) < 2 or now - points[-1][0] > 120:
        return percent, 'Estimating remaining time'
    elapsed = points[-1][0] - points[0][0]
    games = points[-1][1] - points[0][1]
    if elapsed < 30 or games <= 0:
        return percent, 'Estimating remaining time'
    seconds = max(1, math.ceil((total - count) * elapsed / games))
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes = math.ceil(seconds / 60)
    if minutes == 60:
        hours, minutes = hours + 1, 0
    if hours == 24:
        days, hours = days + 1, 0
    duration = ('%dd %dh' % (days, hours) if days else '%dh %dm' % (hours, minutes) if hours else '%dm' % max(1, minutes))
    return percent, 'Estimated remaining: ' + duration
