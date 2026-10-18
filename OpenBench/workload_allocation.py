import math


def record_trend(workload, previous_games, previous_llr):
    if workload.test_mode != 'SPRT' or workload.games <= previous_games:
        return
    if not all(math.isfinite(value) for value in (previous_llr, workload.currentllr)):
        return
    state = workload.execution.get('llr_allocation', {})
    if state.get('execution') != workload.execution_number:
        state = {'execution': workload.execution_number, 'games': previous_games, 'llr': previous_llr, 'trend': 0}
    games = workload.games - state['games']
    if games >= 100:
        slope = 1000 * (workload.currentllr - state['llr']) / games
        state = {'execution': workload.execution_number, 'games': workload.games,
                 'llr': workload.currentllr, 'trend': 0.75 * state['trend'] + 0.25 * max(-3, min(3, slope))}
    workload.execution = {**workload.execution, 'llr_allocation': state}


def effective_throughput(workload):
    throughput = max(1, workload.throughput)
    if workload.test_mode != 'SPRT' or not workload.execution.get('adaptive_throughput'):
        return throughput
    state = workload.execution.get('llr_allocation', {})
    trend = state.get('trend', 0) if state.get('execution') == workload.execution_number else 0
    score = (workload.currentllr + trend) / max(1, abs(workload.lowerllr), abs(workload.upperllr))
    if not math.isfinite(score):
        return throughput
    score = max(-1, min(1, score)) * min(1, workload.games / 1000)
    return throughput * 2 ** score


def worker_threads(machine, workload):
    fallback = machine.info.get('concurrency', 1)
    if workload.execution.get('workflow'):
        from OpenBench.worker_policy import allocation
        selected = allocation(machine.info, workload.execution.get('worker_policy', {}), fallback)
        if selected:
            return selected['threads']
    if not machine.info.get('thread_budgets'):
        return fallback
    key = 'datagen_threads' if workload.test_mode == 'DATAGEN' else 'test_threads'
    value = machine.info.get('worker_settings', {}).get(key, machine.info.get(key, fallback))
    maximum = machine.info.get('logical_cores', fallback)
    return max(1, min(maximum, value)) if type(value) is int else fallback
