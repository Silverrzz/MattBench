import copy
import re

from django.core.exceptions import ValidationError

from OpenBench.bullet import BULLET_REPOSITORY, DEFAULT_BULLET_COMMIT, pinned_bullet_commit
from OpenBench.training_adapters import ADAPTERS, DATA_TOOLS, INPUT_FEATURES


BUILTINS = {}
for key, adapter in ADAPTERS.items():
    provider = DATA_TOOLS[adapter['data_tool']]
    tool = {'kind': adapter['data_tool'], **{name: provider[name] for name in ('repo', 'commit')}}
    if provider['combiner']:
        tool['combiner'] = copy.deepcopy(provider['combiner_defaults'])
    BUILTINS[key] = {'schema': 2, 'name': adapter['name'], 'version': 1, 'generator': key, 'variant': adapter['variant'],
                     'bullet_repo': adapter['bullet_repo'], 'bullet_ref': adapter['bullet_ref'], 'format': adapter['format'],
                     'features': adapter['features'][:], 'optimizers': adapter['optimizers'][:], 'data_tool': tool,
                     'defaults': {}, 'training_settings': {}}
    if adapter.get('rust_minimum'):
        BUILTINS[key]['rust_minimum'] = adapter['rust_minimum']


def validate_profile(settings):
    if not isinstance(settings, dict) or not isinstance(settings.get('generator'), str) or settings['generator'] not in ADAPTERS:
        raise ValidationError('Choose a registered schedule adapter.')
    generator = settings['generator']
    source = copy.deepcopy(BUILTINS[generator])
    allowed = set(source) - {'name', 'version', 'schema', 'format'}
    allowed.add('rust_minimum')
    if set(settings) - (set(source) | {'rust_minimum'}):
        raise ValidationError('Unknown training profile setting.')
    if settings.get('schema', 1) not in (1, 2) or type(settings.get('schema', 1)) is not int:
        raise ValidationError('Unsupported training profile schema.')
    if settings.get('schema', 1) == 1:
        allowed -= {'features', 'optimizers'}
    source.update({key: settings[key] for key in allowed if key in settings})
    adapter = ADAPTERS[generator]
    if settings.get('format', adapter['format']) != adapter['format']:
        raise ValidationError('The dataset format must match the schedule adapter.')
    if not isinstance(source['variant'], str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', source['variant']):
        raise ValidationError('Use a variant identifier of at most 64 lowercase letters, digits, underscores or dashes.')
    for key in ('features', 'optimizers'):
        values = source[key]
        if not isinstance(values, list) or not values or any(not isinstance(value, str) or value not in adapter[key] for value in values) or len(values) != len(set(values)):
            raise ValidationError('Choose supported %s for this schedule adapter.' % key)
    if not any(INPUT_FEATURES[key]['input'] for key in source['features']):
        raise ValidationError('Make at least one input feature available.')
    minimum = source.get('rust_minimum', '')
    if not isinstance(minimum, str) or minimum and not re.fullmatch(r'\d+\.\d+\.\d+', minimum):
        raise ValidationError('Use a Rust version such as 1.95.0.')
    required = BUILTINS[generator].get('rust_minimum')
    if required and (not minimum or tuple(map(int, minimum.split('.'))) < tuple(map(int, required.split('.')))):
        raise ValidationError('This adapter requires Rust %s or newer.' % required)
    if not isinstance(source['bullet_repo'], str) or not re.fullmatch(r'https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', source['bullet_repo']):
        raise ValidationError('Use an HTTPS GitHub Bullet repository.')
    if not isinstance(source['bullet_ref'], str) or not re.fullmatch(r'[0-9a-f]{40}', source['bullet_ref']):
        raise ValidationError('Pin the profile to a full Bullet commit.')
    tool = source['data_tool']
    if not isinstance(tool, dict) or set(tool) - {'kind', 'repo', 'commit', 'combiner'}:
        raise ValidationError('Choose a valid dataset tool provider.')
    tool = {**copy.deepcopy(BUILTINS[generator]['data_tool']), **tool}
    provider = DATA_TOOLS.get(tool.get('kind')) if isinstance(tool.get('kind'), str) else None
    if not provider or source['format'] not in provider['formats'] or not provider['combiner'] and 'combiner' in tool:
        raise ValidationError('The dataset tools must match the profile’s format.')
    for item in [tool, *([tool.get('combiner')] if provider['combiner'] else [])]:
        if not isinstance(item, dict) or not isinstance(item.get('repo'), str) or not isinstance(item.get('commit'), str) or not re.fullmatch(r'https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', item['repo']) or not re.fullmatch(r'[0-9a-f]{40}', item['commit']):
            raise ValidationError('Pin dataset tools to GitHub repositories and full commits.')
    source['data_tool'] = tool
    from OpenBench.schedule_builder import DEFAULT_SPEC
    from OpenBench.training import DEFAULT_SETTINGS
    defaults = source['defaults']
    if not isinstance(defaults, dict) or set(defaults) - (set(DEFAULT_SPEC) - {'training_profile'}):
        raise ValidationError('Choose supported schedule defaults.')
    runtime = source['training_settings']
    managed = {'training_profile', 'bullet_repo', 'bullet_ref', 'dataset_format', 'backend', 'threads', 'shuffle', 'shuffle_seed', 'interleave', 'shuffle_memory_mb', 'interleave_fan_in', 'dataset_shard_mb'}
    if not isinstance(runtime, dict) or set(runtime) - (set(DEFAULT_SETTINGS) - managed):
        raise ValidationError('Choose supported training runtime settings. Network and loader defaults belong in schedule defaults.')
    try:
        import json
        if len(json.dumps([defaults, runtime], allow_nan=False)) > 65536:
            raise ValueError
    except (ValueError, TypeError):
        raise ValidationError('Profile defaults must be finite JSON values under 64 KiB.') from None
    default_spec(source)
    return source


def validate_profile_spec(spec, profile):
    adapter = ADAPTERS[profile['generator']]
    features = profile['features'] if profile.get('schema') == 2 else adapter['features']
    for key in INPUT_FEATURES:
        if spec[key] and key not in features:
            raise ValidationError('%s is unavailable in this training profile.' % INPUT_FEATURES[key]['label'])
    if spec['optimizer'] not in profile['optimizers']:
        raise ValidationError('Choose an optimizer available in this training profile.')
    for key, value in adapter['fixed'].items():
        if spec[key] != value:
            raise ValidationError('%s requires %s = %s.' % (adapter['label'], key.replace('_', ' '), value))
    limits = adapter['layers']
    if not limits['minimum'] <= len(spec['layers']) <= limits['maximum']:
        raise ValidationError('%s requires %d–%d hidden layers.' % (adapter['label'], limits['minimum'], limits['maximum']))
    for index, multiple in limits['multiples'].items():
        if spec['layers'][int(index)] % multiple:
            raise ValidationError('Layer %d must be divisible by %d for %s.' % (int(index) + 1, multiple, adapter['label']))


def default_spec(profile):
    from OpenBench.schedule_builder import DEFAULT_SPEC, validate_spec
    adapter = ADAPTERS[profile['generator']]
    spec = {**copy.deepcopy(DEFAULT_SPEC), **copy.deepcopy(adapter['defaults']), **copy.deepcopy(adapter['fixed'])}
    features = profile['features'] if profile.get('schema') == 2 else adapter['features']
    spec.update({key: False for key in INPUT_FEATURES if key not in features})
    spec['optimizer'] = profile['optimizers'][0]
    overrides = copy.deepcopy(profile.get('defaults', {}))
    spec.update(overrides)
    inputs = [key for key in features if INPUT_FEATURES[key]['input']]
    if not any(spec[key] for key in inputs) and not any(key in overrides for key in inputs):
        spec[inputs[0]] = True
    if 'superbatches' in overrides:
        for channel in ('lr_stages', 'wdl_stages'):
            if channel not in overrides:
                spec[channel][-1]['end'] = overrides['superbatches']
    spec['training_profile'] = profile.get('id', 'standard')
    spec = validate_spec(spec)
    validate_profile_spec(spec, profile)
    return spec


def profile_data(profile):
    adapter = ADAPTERS[profile['generator']]
    features = profile['features'] if profile.get('schema') == 2 else adapter['features']
    return {**profile, 'features': features[:], 'builder': copy.deepcopy({key: adapter[key] for key in ('label', 'fixed', 'hidden', 'position_fields', 'layers', 'export_note')}),
            'input_features': [{'key': key, **INPUT_FEATURES[key]} for key in features], 'schedule_defaults': default_spec(profile),
            'runtime_settings': profile_runtime(profile),
            'tool_label': DATA_TOOLS[profile['data_tool']['kind']]['label']}


def profiles():
    from OpenBench.models import TrainingProfile
    rows = {row.pk: row for row in TrainingProfile.objects.all()}
    result = []
    for identifier in dict.fromkeys([*BUILTINS, *rows]):
        row = rows.get(identifier)
        if row is not None and not row.enabled:
            continue
        settings = validate_profile(row.settings if row else BUILTINS[identifier])
        settings.update(id=identifier, name=row.name if row else BUILTINS[identifier]['name'], version=row.version if row else 1)
        if identifier == 'standard' and settings['bullet_repo'] == BULLET_REPOSITORY and settings['bullet_ref'] == DEFAULT_BULLET_COMMIT:
            settings['bullet_ref'] = pinned_bullet_commit()
        result.append(settings)
    return result


def resolve_profile(identifier='standard', snapshot=None):
    if snapshot is not None:
        if snapshot.get('id') != identifier:
            raise ValidationError('The saved training profile does not match this schedule.')
        validate_profile({key: value for key, value in snapshot.items() if key != 'id'})
        return copy.deepcopy(snapshot)
    profile = next((item for item in profiles() if item['id'] == identifier), None)
    if profile is None:
        raise ValidationError('This training profile is unavailable.')
    return profile


def profile_settings(profile):
    return {**copy.deepcopy(profile.get('training_settings', {})), 'training_profile': copy.deepcopy(profile), 'bullet_repo': profile['bullet_repo'],
            'bullet_ref': profile['bullet_ref'], 'dataset_format': profile['format']}


def profile_runtime(profile, spec=None, resume_supported=False):
    from OpenBench.training import DEFAULT_SETTINGS
    spec = spec if spec is not None else default_spec(profile)
    config = {**copy.deepcopy(DEFAULT_SETTINGS), 'backend': spec['backend'], 'threads': spec['threads'],
            'build': [*DEFAULT_SETTINGS['build'][:-1], spec['backend']], 'resume_supported': resume_supported,
            'shuffle_seed': spec['seed'], **{key: spec[key] for key in ('shuffle', 'interleave', 'shuffle_memory_mb', 'interleave_fan_in', 'dataset_shard_mb')},
            **profile_settings(profile)}
    runtime = profile.get('training_settings', {})
    if config['example_name']:
        if 'build' not in runtime:
            config['build'][config['build'].index('--example') + 1] = config['example_name']
        if 'run' not in runtime:
            config['run'] = ['target/release/examples/' + config['example_name']]
    return config


def profile_sources(files, settings):
    from pathlib import PurePosixPath
    from OpenBench.schedule_builder import SOURCE
    from OpenBench.training import relative_path
    target = relative_path(settings['example_source'])
    if target == SOURCE:
        return files
    if not target.endswith('.rs') or target in files:
        raise ValidationError('Choose a separate Rust entry-point path for generated schedules.')
    parent = PurePosixPath(target).parent
    if any(path != SOURCE and PurePosixPath(path).parent == PurePosixPath(SOURCE).parent and str(parent / PurePosixPath(path).name) == target for path in files):
        raise ValidationError('The entry-point path conflicts with a generated support module.')
    files[target] = files[SOURCE]
    for path, source in list(files.items()):
        if path != SOURCE and PurePosixPath(path).parent == PurePosixPath(SOURCE).parent and path.endswith('.rs'):
            files[str(parent / PurePosixPath(path).name)] = source
    return files
