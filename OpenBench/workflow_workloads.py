import math
from types import SimpleNamespace

from django.core.exceptions import ValidationError

from OpenBench.models import Engine, EngineConfig, Network, OpeningBook, Test, WorkloadPreset
from OpenBench.utils import TimeControl


def defaults(engine, kind, variant):
    source = engine.settings.get('source', '')
    books = list(OpeningBook.objects.filter(enabled=True, variants__name=variant).values_list('name', flat=True))
    values = {'dev_engine': engine.name, 'base_engine': engine.name, 'dev_repo': source, 'base_repo': source,
              'dev_branch': 'master', 'base_branch': 'master', 'dev_bench': '', 'base_bench': '',
              'dev_network': '', 'base_network': '', 'dev_options': 'Threads=1 Hash=16', 'base_options': 'Threads=1 Hash=16',
              'dev_time_control': '10.0+0.1', 'base_time_control': '10.0+0.1', 'book_name': books[0] if books else 'NONE',
              'upload_pgns': 'COMPACT' if kind == 'datagen' else 'FALSE', 'priority': '0', 'throughput': '1000',
              'workload_size': '32', 'syzygy_wdl': 'OPTIONAL', 'syzygy_adj': 'OPTIONAL',
              'win_adj': 'movecount=3 score=400', 'draw_adj': 'movenumber=40 movecount=8 score=10',
              'scale_method': 'BASE', 'scale_nps': '1000000', 'test_mode': 'SPRT', 'test_bounds': '[0.0, 5.0]',
              'test_confidence': '[0.05, 0.05]', 'test_max_games': '0', 'datagen_max_games': '10000000',
              'datagen_custom_genfens': '', 'datagen_play_reverses': 'YES', 'info': '', 'variant': variant,
              'spsa_inputs': '', 'spsa_alpha': '0.602', 'spsa_gamma': '0.101', 'spsa_A_ratio': '0.1', 'spsa_iterations': '100000', 'spsa_pairs_per': '32', 'spsa_reporting_type': 'BATCHED', 'spsa_distribution_type': 'SINGLE'}
    preset = WorkloadPreset.objects.filter(engine=engine, owner=None, workload_type={'datagen': 'DATAGEN', 'test': 'TEST', 'tune': 'TUNE'}[kind]).order_by('position', 'name').first()
    if preset:
        for key, value in preset.settings.items():
            if key.startswith('both_'):
                for side in ('dev', 'base'):
                    values[side + '_' + key[5:]] = str(value)
            else:
                values[key] = str(value)
    values.update(dev_engine=engine.name, variant=variant)
    return values


def prepare(node, engine, profile, user):
    from OpenBench.workloads.verify_workload import verify_workload
    from OpenBench.workflows import workload_snapshot
    kind, config = node['kind'], node['config']
    parameters = config.get('parameters', {})
    values = defaults(engine, kind, profile['variant'])
    if not isinstance(parameters, dict) or set(parameters) - (set(values) | {'capture_logs', 'adaptive_throughput'}):
        raise ValidationError('Invalid workload configuration.')
    if any(type(value) not in (str, int, float, bool) or len(str(value)) > (262144 if key == 'spsa_inputs' else 4096) for key, value in parameters.items()):
        raise ValidationError('Invalid workload configuration value.')
    values.update({key: str(value) for key, value in parameters.items()})
    values['dev_engine'] = engine.name
    if kind == 'datagen':
        if 'datagen_max_games' not in parameters:
            values['datagen_max_games'] = str(config.get('games', 10000000))
    elif kind == 'test':
        control = config.get('control', 'template' if parameters else 'stc')
        if control != 'template' and 'dev_time_control' not in parameters:
            values['dev_time_control'] = values['base_time_control'] = 'N=%s' % config.get('nodes', 25000) if control == 'nodes' else config.get('time_control') or ('60.0+0.6' if control == 'ltc' else '10.0+0.1')
    if kind == 'tune':
        values['spsa_inputs'] = '\n'.join(line.strip() for line in values['spsa_inputs'].splitlines() if line.strip())
    request = SimpleNamespace(POST=values, user=user)
    errors, branches = verify_workload(request, {'datagen': 'DATAGEN', 'test': 'TEST', 'tune': 'TUNE'}[kind])
    if errors:
        raise ValidationError(node['name'] + ': ' + '; '.join(errors))
    if kind == 'tune':
        branches = (branches, branches)
        for key in list(values):
            if key.startswith('base_'):
                values[key] = values.get('dev_' + key[5:], values[key])
        config['spsa'] = {key: value for key, value in values.items() if key.startswith('spsa_')}
        if any(float(values[key]) <= 0 for key in ('spsa_A_ratio', 'spsa_alpha', 'spsa_gamma', 'spsa_iterations', 'spsa_pairs_per')):
            raise ValidationError('SPSA settings must be positive.')
    test = Test(author=user.username, info=branches[0][4] if kind == 'test' else values['info'], test_mode={'datagen': 'DATAGEN', 'test': values['test_mode'], 'tune': 'SPSA'}[kind])
    for side, info in zip(('dev', 'base'), branches):
        setattr(test, side, Engine(source=info[0], name=info[1], sha=info[2], bench=info[3]))
        for suffix in ('repo', 'engine', 'options', 'network'):
            setattr(test, side + '_' + suffix, values[side + '_' + suffix])
        setattr(test, side + '_time_control', TimeControl.parse(values[side + '_time_control']))
        net = Network.objects.filter(engine=values[side + '_engine'], sha256=values[side + '_network']).first()
        setattr(test, side + '_netname', net.name if net else '')
    for key in ('book_name', 'upload_pgns', 'syzygy_wdl', 'syzygy_adj', 'win_adj', 'draw_adj', 'scale_method'):
        setattr(test, key, values[key])
    for key in ('priority', 'throughput', 'workload_size', 'scale_nps'):
        setattr(test, key, int(values[key]))
    test.execution = {**request.workload_execution, 'capture_logs': values.get('capture_logs', 'on') == 'on', 'adaptive_throughput': values.get('adaptive_throughput') == 'on'}
    if kind == 'datagen':
        test.max_games = int(values['datagen_max_games'])
        test.genfens_args = values['datagen_custom_genfens']
        test.play_reverses = values['datagen_play_reverses'] == 'YES'
        test.use_tri, test.use_penta = not test.play_reverses, test.play_reverses
    elif kind == 'tune':
        test.workload_size = int(values['spsa_pairs_per'])
    elif test.test_mode == 'GAMES':
        test.max_games = int(values['test_max_games'])
    else:
        test.elolower, test.eloupper = map(float, values['test_bounds'].strip('[]').split(','))
        test.alpha, test.beta = map(float, values['test_confidence'].strip('[]').split(',')[::-1])
        test.lowerllr, test.upperllr = math.log(test.beta / (1 - test.alpha)), math.log((1 - test.beta) / test.alpha)
    return workload_snapshot(test)
