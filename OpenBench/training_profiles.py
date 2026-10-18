import copy
import re

from django.core.exceptions import ValidationError

from OpenBench.bullet import BULLET_REPOSITORY, DEFAULT_BULLET_COMMIT, pinned_bullet_commit


DUCK_COMMIT = '6629e9bc9b205d4d0dea25a24aad4234a0b4b085'
WAKFORMAT_COMMIT = '50f99df368b3753b74b2dfbd9d38822d8cd5118f'
BUILTINS = {
    'standard': {'name': 'Standard chess', 'version': 1, 'generator': 'standard', 'variant': 'standard',
                 'bullet_repo': BULLET_REPOSITORY, 'bullet_ref': DEFAULT_BULLET_COMMIT, 'format': 'vf',
                 'features': ['psqt_inputs', 'threat_inputs', 'pawn_pair_inputs', 'half_move_clock', 'merged_king_planes'],
                 'optimizers': ['adamw', 'ranger']},
    'duckchess': {'name': 'DuckChess', 'version': 1, 'generator': 'duckchess', 'variant': 'duck',
                  'bullet_repo': 'https://github.com/Silverrzz/bullet', 'bullet_ref': DUCK_COMMIT, 'format': 'wf',
                  'features': ['psqt_inputs', 'duck_inputs'], 'optimizers': ['adamw'],
                  'rust_minimum': '1.95.0',
                  'data_tool': {'repo': 'https://github.com/Silverrzz/wakformat', 'commit': WAKFORMAT_COMMIT}},
}


def validate_profile(settings):
    if not isinstance(settings, dict) or settings.get('generator') not in ('standard', 'duckchess'):
        raise ValidationError('Choose the standard or DuckChess generator.')
    generator = settings['generator']
    source = copy.deepcopy(BUILTINS[generator])
    allowed = set(source) - {'name', 'version', 'features', 'optimizers', 'format', 'variant', 'rust_minimum'}
    if set(settings) - set(source):
        raise ValidationError('Unknown training profile setting.')
    source.update({key: settings[key] for key in allowed if key in settings})
    if not isinstance(source['bullet_repo'], str) or not re.fullmatch(r'https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', source['bullet_repo']):
        raise ValidationError('Use an HTTPS GitHub Bullet repository.')
    if not isinstance(source['bullet_ref'], str) or not re.fullmatch(r'[0-9a-f]{40}', source['bullet_ref']):
        raise ValidationError('Pin the profile to a full Bullet commit.')
    if generator == 'duckchess':
        tool = source['data_tool']
        if not isinstance(tool, dict) or set(tool) != {'repo', 'commit'} or not isinstance(tool['repo'], str) or not isinstance(tool['commit'], str) or not re.fullmatch(r'https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', tool['repo']) or not re.fullmatch(r'[0-9a-f]{40}', tool['commit']):
            raise ValidationError('Pin the Wakformat tool to a GitHub repository and full commit.')
    return source


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
    return {'training_profile': copy.deepcopy(profile), 'bullet_repo': profile['bullet_repo'],
            'bullet_ref': profile['bullet_ref'], 'dataset_format': profile['format']}
