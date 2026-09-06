from django.core.exceptions import ValidationError
from jsonschema import Draft202012Validator


DEFAULT_SITE = {
    'client_version': 49,
    'client_repo_url': 'https://github.com/AndyGrant/OpenBench',
    'client_repo_ref': 'master',
    'fastchess_min_version': '1.8.1',
    'fastchess_repo_url': 'https://github.com/AndyGrant/fastchess',
    'fastchess_repo_ref': 'master',
    'use_cross_approval': False,
    'require_login_to_view': False,
    'require_manual_registration': False,
    'balance_engine_throughputs': True,
    'use_x_accel_redirect': False,
    'x_accel_redirect_root': '/x-accel-media/',
}


def document(properties, required=()):
    return {'type': 'object', 'properties': properties, 'required': list(required), 'additionalProperties': False}


STRING = {'type': 'string'}
STRINGS = {'type': 'array', 'items': STRING, 'uniqueItems': True}
URL = {'type': 'string', 'pattern': r'^https://[^\s]+$'}
BUILD = document({key: STRINGS for key in ('compilers', 'systems', 'cpuflags')} | {'path': STRING}, ('compilers', 'systems', 'cpuflags', 'path'))
SCHEMAS = {
    'sitesettings': document({key: {'type': 'boolean' if type(value) is bool else 'integer' if type(value) is int else 'string'} for key, value in DEFAULT_SITE.items()}, DEFAULT_SITE),
    'engineconfig': document({'private': {'type': 'boolean'}, 'nps': {'type': 'integer', 'minimum': 0}, 'source': STRING, 'build': BUILD}, ('private', 'nps', 'source', 'build')),
    'runner': document({'source': URL, 'build': document({'path': STRING, 'target': STRING})}, ('source',)),
    'runnerrelease': document({'ref': {'type': 'string', 'minLength': 1}, 'commit': {'type': 'string', 'pattern': '^[0-9a-f]{40}$'}, 'min_version': {'type': 'string', 'pattern': r'^\d+\.\d+(\.\d+)?$'}, 'protocol': {'enum': ['fastchess-ob']}}, ('ref', 'min_version', 'protocol')),
    'variant': document({'fastchess_variant': {'type': 'string', 'pattern': '^[a-zA-Z0-9_-]+$'}, 'syzygy': {'type': 'boolean'}}, ('fastchess_variant', 'syzygy')),
    'openingbook': document({'source': URL, 'sha': {'type': 'string', 'pattern': '^[a-fA-F0-9]{64}$'}, 'format': {'enum': ['epd', 'pgn']}}, ('source', 'sha', 'format')),
}


def validate_settings(kind, value, version=1):
    if version != 1 or kind not in SCHEMAS:
        raise ValidationError('Unsupported configuration schema: %s version %s' % (kind, version))
    errors = sorted(Draft202012Validator(SCHEMAS[kind]).iter_errors(value), key=lambda error: str(error.path))
    if errors:
        raise ValidationError(['%s: %s' % ('.'.join(map(str, error.path)) or kind, error.message) for error in errors])


def validate_preset(workload_type, value, version=1):
    from OpenBench.config import verify_engine_test_preset, verify_engine_tune_preset, verify_engine_datagen_preset
    validators = {'TEST': verify_engine_test_preset, 'TUNE': verify_engine_tune_preset, 'DATAGEN': verify_engine_datagen_preset}
    if version != 1 or workload_type not in validators or not isinstance(value, dict):
        raise ValidationError('Invalid preset schema')
    if any(type(item) not in (str, int, float, bool) for item in value.values()):
        raise ValidationError('Preset fields must contain scalar values')
    try:
        validators[workload_type](value)
    except Exception as error:
        raise ValidationError(str(error)) from error
