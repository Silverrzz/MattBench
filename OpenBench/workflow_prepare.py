import math
import re

from django.core.exceptions import ValidationError

from OpenBench.models import EngineConfig, Test


def prepare(node, definition, user):
    from OpenBench.training_profiles import resolve_profile
    config, kind = node['config'], node['kind']
    profile = resolve_profile(config.get('profile') or definition['profile'])
    config['training_profile'] = profile
    config['engine'] = str(config.get('engine') or definition.get('engine') or '')
    engine = EngineConfig.objects.filter(pk=config['engine'], enabled=True).first() if config['engine'] else None
    if kind in ('datagen', 'test', 'tune', 'train') and engine is None:
        raise ValidationError(node['name'] + ': choose an engine.')
    if kind in ('datagen', 'test', 'tune'):
        if kind != 'tune' and config.get('template') and 'parameters' not in config:
            from OpenBench.workflows import workload_snapshot
            expected = {'datagen': 'DATAGEN', 'test': 'SPRT', 'tune': 'SPSA'}[kind]
            row = Test.objects.filter(pk=config['template'], deleted=False, test_mode=expected).first()
            if row is None or row.dev_engine != engine.name or row.execution.get('demo') or row.execution.get('fastchess_variant', 'standard') != profile['variant']:
                raise ValidationError('The workload configuration is unavailable or incompatible.')
            config['workload'] = workload_snapshot(row)
        else:
            from OpenBench.workflow_workloads import prepare as workload
            config['workload'] = workload(node, engine, profile, user)
        if kind == 'datagen' and config['workload']['upload_pgns'] == 'FALSE':
            raise ValidationError('Enable PGN uploads for a workflow datagen node to produce its dataset output.')
        if kind == 'datagen' and 'parameters' not in config:
            games = config.get('games', 10000000)
            if type(games) is not int or games < 2 or games > 10000000000 or games % 2:
                raise ValidationError('Use an even datagen game target between 2 and 10 billion.')
        elif kind == 'test':
            if config.get('control', 'stc') not in ('template', 'nodes', 'stc', 'ltc'):
                raise ValidationError('Choose fixed nodes, STC or LTC.')
            if config.get('control') == 'nodes' and (type(config.get('nodes')) is not int or not 1 <= config['nodes'] <= 1000000000000):
                raise ValidationError('Choose a positive node limit.')
            if config.get('max_games', 0) and (type(config['max_games']) is not int or not 2 <= config['max_games'] <= 10000000000):
                raise ValidationError('Choose a valid SPRT game cap.')
    elif kind == 'train':
        from OpenBench.schedule_builder import builder_state, generate_schedule
        from OpenBench.training import validate_schedule
        from OpenBench.training_profiles import profile_runtime
        if 'files' in config:
            if not isinstance(config.get('settings', {}), dict):
                raise ValidationError('Use an object for the training runtime settings.')
            options = {**profile_runtime(profile), **config.get('settings', {}), 'training_profile': profile,
                       'bullet_repo': profile['bullet_repo'], 'bullet_ref': profile['bullet_ref'], 'dataset_format': profile['format']}
            options = validate_schedule(config['files'], options)
            config['schedule_snapshot'] = {'name': node['name'], 'version': 1, 'files': config['files'], 'settings': options, 'environment': {}}
            from OpenBench.workflow_training import prepare as training
            training(node, user)
            return
        if config.get('spec'):
            spec, name, version = config['spec'], node['name'], 1
        else:
            from OpenBench.training_views import schedules_for
            schedule = schedules_for(user).filter(pk=config.get('schedule')).first()
            if not schedule or 'mattbench-demo.json' in schedule.files or schedule.engine_id and schedule.engine_id != engine.pk:
                raise ValidationError('Choose a compatible runnable schedule, or configure this node’s schedule.')
            captured = schedule.settings.get('training_profile') or None
            if captured:
                profile = resolve_profile(profile['id'], captured)
                config['training_profile'] = profile
            spec, managed = builder_state(schedule)
            if not managed:
                options = validate_schedule(schedule.files, schedule.settings)
                if not captured:
                    if any(options[key] != profile[key] for key in ('bullet_repo', 'bullet_ref')) or options['dataset_format'] != profile['format']:
                        raise ValidationError('The source schedule must match the selected profile’s trainer and data format.')
                    options['training_profile'] = profile
                config['schedule_snapshot'] = {'name': schedule.name, 'version': schedule.version, 'files': schedule.files, 'settings': options, 'environment': {}}
                from OpenBench.workflow_training import prepare as training
                training(node, user)
                return
            name, version = schedule.name, schedule.version
        if spec.get('training_profile', 'standard') != profile['id']:
            raise ValidationError('The schedule and node profiles must match.')
        _, files, options = generate_schedule(spec, profile['bullet_ref'], profile)
        config['schedule_snapshot'] = {'name': name, 'version': version, 'files': files, 'settings': options, 'environment': {}}
        from OpenBench.workflow_training import prepare as training
        training(node, user)
    elif kind == 'upload':
        from OpenBench.training import hf_token, repo_id
        hf_token(user.pk)
        config['repo'] = repo_id(config.get('repo', ''))
        if type(config.get('private', True)) is not bool:
            raise ValidationError('Choose the upload visibility.')
        if not isinstance(config.get('branch', 'main'), str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.\-/]{0,199}', config.get('branch', 'main')):
            raise ValidationError('Choose a valid destination branch.')
    elif kind == 'decision':
        if not isinstance(config.get('field'), str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_.]{0,127}', config['field']) or config.get('operator') not in ('gt', 'gte', 'lt', 'lte', 'eq') or type(config.get('value')) not in (int, float) or not math.isfinite(config['value']):
            raise ValidationError('Choose a metric, comparison and finite numeric value.')
    elif kind.startswith('load_'):
        from OpenBench import workflow_assets as assets
        functions = {'load_data': assets.dataset_source, 'load_parameters': assets.parameters_source, 'load_checkpoint': assets.checkpoint_source, 'load_artifact': assets.training_artifact}
        if kind == 'load_network':
            assets.network_source(config)
        else:
            functions[kind](config, user)
    elif kind in ('convert', 'split'):
        from OpenBench.training import DEFAULT_SETTINGS
        for key, minimum, maximum in (('dataset_shard_mb', 4, 16384), ('seed', 0, 2 ** 63 - 1)):
            value = config.get(key, DEFAULT_SETTINGS.get(key, 42))
            if type(value) is not int or not minimum <= value <= maximum:
                raise ValidationError('Invalid data operation setting: ' + key)
        if kind == 'split':
            fraction = config.get('validation_fraction', .1)
            if type(fraction) not in (int, float) or not 0 < fraction < 1:
                raise ValidationError('The validation fraction must be between zero and one.')
        for key in ('shuffle', 'skip_broken_games'):
            if type(config.get(key, True)) is not bool:
                raise ValidationError('Invalid data operation option.')
    elif kind == 'task':
        from OpenBench.workflow_tasks import validate
        validate(node)
