import math


MAX_HISTORY_POINTS = 200


def history_state(games):
    state = {'count': len(games), 'last_games': games[-1] if games else 0}
    if len(games) > 1:
        gap, remove_games = min((right - left, right) for left, right in zip(games, games[1:]))
        state.update(min_gap=gap, remove_games=remove_games)
    return state


def record_llr_history(workload, previous_games, previous_llr):
    if workload.test_mode != 'SPRT' or workload.games <= previous_games:
        return
    if not math.isfinite(workload.currentllr):
        return
    state = workload.llr_history_state
    if state and state['count'] >= MAX_HISTORY_POINTS and workload.games - state['last_games'] <= state['min_gap']:
        return
    history = workload.llr_history
    if not state:
        games = list(history.values_list('games', flat=True)[:MAX_HISTORY_POINTS])
        state = history_state(games)
    if not state['count'] and math.isfinite(previous_llr):
        history.create(games=previous_games, llr=previous_llr)
        state = history_state([previous_games])
    if state['count'] >= MAX_HISTORY_POINTS:
        if workload.games - state['last_games'] <= state['min_gap']:
            workload.llr_history_state = state
            return
        history.filter(games=state['remove_games']).update(games=workload.games, llr=workload.currentllr)
        workload.llr_history_state = history_state(list(history.values_list('games', flat=True)[:MAX_HISTORY_POINTS]))
    else:
        history.create(games=workload.games, llr=workload.currentllr)
        state = dict(state)
        gap = workload.games - state['last_games']
        if state['count'] and ('min_gap' not in state or gap < state['min_gap']):
            state.update(min_gap=gap, remove_games=workload.games)
        state.update(count=state['count'] + 1, last_games=workload.games)
        workload.llr_history_state = state


def workload_llr_history(workload):
    points = list(workload.llr_history.filter(games__lte=workload.games).values('games', 'llr')[:MAX_HISTORY_POINTS])
    current = {'games': workload.games, 'llr': workload.currentllr}
    if not points or points[-1]['games'] != workload.games:
        if len(points) == MAX_HISTORY_POINTS:
            index = min(range(1, len(points)), key=lambda i: points[i]['games'] - points[i - 1]['games'])
            points.pop(index)
        points.append(current)
    else:
        points[-1] = current
    points = [point for point in points if math.isfinite(point['llr'])]
    return {
        'points': points,
        'lower': workload.lowerllr,
        'upper': workload.upperllr,
        'games': workload.games,
        'llr': workload.currentllr,
        'finished': workload.finished or workload.deleted,
        'status': ('Passed' if workload.passed else 'Failed' if workload.failed else
                   'Deleted' if workload.deleted else 'Stopped' if workload.finished else
                   'Running' if workload.approved else 'Awaiting approval'),
    }
