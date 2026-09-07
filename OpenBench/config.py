# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #
#                                                                             #
#   OpenBench is a chess engine testing framework authored by Andrew Grant.   #
#   <https://github.com/AndyGrant/OpenBench>           <andrew@grantnet.us>   #
#                                                                             #
#   OpenBench is free software: you can redistribute it and/or modify         #
#   it under the terms of the GNU General Public License as published by      #
#   the Free Software Foundation, either version 3 of the License, or         #
#   (at your option) any later version.                                       #
#                                                                             #
#   OpenBench is distributed in the hope that it will be useful,              #
#   but WITHOUT ANY WARRANTY; without even the implied warranty of            #
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the             #
#   GNU General Public License for more details.                              #
#                                                                             #
#   You should have received a copy of the GNU General Public License         #
#   along with this program.  If not, see <http://www.gnu.org/licenses/>.     #
#                                                                             #
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

import copy, hashlib, json, re

from collections.abc import Mapping
from contextvars import ContextVar
from pathlib import Path
from django.conf import settings
from django.core.exceptions import ValidationError


_request_config = ContextVar('openbench_config', default=None)


def read_site_config():

    with (Path(settings.BASE_DIR) / 'Config' / 'config.json').open(encoding='utf-8-sig') as stream:
        config = json.load(stream)
    config.pop('engines', None)
    config.pop('books', None)
    config.setdefault('variants', {'standard': {}, 'fischerandom': {'syzygy': True}})
    for name, variant in config['variants'].items():
        variant.setdefault('fastchess_variant', name)
        variant.setdefault('syzygy', name in ('standard', 'fischerandom'))
        variant.setdefault('runner', {key: config['fastchess_' + key] for key in ('repo_url', 'repo_ref', 'min_version')})
    return config


def load_config():

    from OpenBench.models import EngineConfig, OpeningBook, Variant

    config = read_site_config()
    config['variants'] = {}
    for variant in Variant.objects.filter(enabled=True, runner_release__enabled=True, runner_release__runner__enabled=True).select_related('runner_release__runner'):
        release = variant.runner_release
        config['variants'][variant.name] = dict(variant.settings, runner={
            'repo_url': release.runner.settings['source'],
            'repo_ref': release.settings.get('commit') or release.settings['ref'],
            'min_version': release.settings['min_version'],
        })
    config['books'] = {book.name: dict(book.settings, variant=book.variant.name, format=book.name.rsplit('.', 1)[-1].lower())
                       for book in OpeningBook.objects.filter(enabled=True).select_related('variant').order_by('name')
                       if book.variant and book.variant.name in config['variants']}
    config['engines'] = {}
    for engine in EngineConfig.objects.filter(enabled=True).order_by('name').prefetch_related('presets', 'variants'):
        data = copy.deepcopy(engine.settings)
        data['variants'] = sorted(variant.name for variant in engine.variants.all() if variant.name in config['variants'])
        if not data['variants']:
            continue
        for kind in ('test_presets', 'tune_presets', 'datagen_presets'):
            data[kind] = {'default': {}}
        for preset in engine.presets.all():
            if preset.owner_id is None:
                kind = {'TEST': 'test_presets', 'TUNE': 'tune_presets', 'DATAGEN': 'datagen_presets'}[preset.workload_type]
                data[kind][preset.name] = preset.settings
        config['engines'][engine.name] = data
    return config


class ConfigMapping(Mapping):

    def current(self):
        config = _request_config.get()
        return config if config is not None else load_config()

    def __getitem__(self, key):
        return self.current()[key]

    def __iter__(self):
        return iter(self.current())

    def __len__(self):
        return len(self.current())


class ConfigurationMiddleware:

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        token = _request_config.set(load_config())
        try:
            return self.get_response(request)
        finally:
            _request_config.reset(token)


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def eligibility_fingerprint():
    return fingerprint({name: {key: data[key] for key in ('build', 'private', 'source', 'variants')}
                        for name, data in OPENBENCH_CONFIG['engines'].items()})


def workload_execution(book_name, engines, variant_name=None):
    config = OPENBENCH_CONFIG
    book = config['books'].get(book_name)
    if book is None and book_name != 'NONE':
        raise ValidationError('Choose an enabled opening book')
    if book and variant_name and variant_name != book['variant']:
        raise ValidationError('The opening book does not match the selected variant')
    name = variant_name or (book['variant'] if book else 'standard')
    variant = config['variants'].get(name)
    if variant is None:
        raise ValidationError('Choose an enabled variant and runner release')
    for engine in engines:
        if name not in config['engines'].get(engine, {}).get('variants', []):
            raise ValidationError('%s does not support %s' % (engine, name))
    return dict(copy.deepcopy(variant), variant=name)


def verify_engine_config(data, enabled=True):

    if not isinstance(data, dict) or not isinstance(data.get('build'), dict):
        raise ValidationError('Engine configuration requires build settings')
    build = data['build']
    if type(data.get('private')) is not bool or type(data.get('nps')) is not int or data['nps'] < 0:
        raise ValidationError('Provide an engine privacy flag and nonnegative reference NPS')
    if not isinstance(data.get('source'), str) or (data['source'] and not re.fullmatch(r'https://[^\s]+', data['source'])):
        raise ValidationError('Engine source must be an HTTPS URL')
    if not isinstance(build.get('path'), str):
        raise ValidationError('Build path must be text; use an empty string for the repository root')
    for field in ('systems', 'compilers', 'cpuflags'):
        values = build.get(field)
        if not isinstance(values, list) or any(not isinstance(value, str) or not value for value in values):
            raise ValidationError('Build %s must be a list of strings' % field)
    if enabled and (not data['nps'] or not data['source'] or not build['systems'] or (not data['private'] and not build['compilers'])):
        raise ValidationError('Enabled engines need a source, positive NPS, operating systems and compilers for public builds')


def verify_book_config(name, data):

    if name.rsplit('.', 1)[-1].lower() not in ('epd', 'pgn'):
        raise ValidationError('Book names must end in .epd or .pgn')
    if not isinstance(data, dict) or not isinstance(data.get('source'), str) or not re.fullmatch(r'https://[^\s]+', data['source']):
        raise ValidationError('Book source must be an HTTPS URL')
    if not isinstance(data.get('sha'), str) or not re.fullmatch('[a-fA-F0-9]{64}', data['sha']):
        raise ValidationError('Book SHA-256 must contain 64 hexadecimal characters')


def verify_preset(kind, data):

    validators = {'TEST': verify_engine_test_preset, 'TUNE': verify_engine_tune_preset, 'DATAGEN': verify_engine_datagen_preset}
    if kind not in validators or not isinstance(data, dict) or any(type(value) not in (str, int, float, bool) for value in data.values()):
        raise ValidationError('Invalid preset settings')
    try:
        validators[kind](data)
    except Exception as error:
        raise ValidationError(str(error)) from error


def verify_runner_config(data):
    if not isinstance(data.get('source'), str) or not re.fullmatch(r'https://[^\s]+', data['source']):
        raise ValidationError('Runner source must be an HTTPS URL')


def verify_release_config(data):
    if not data.get('ref') or not re.fullmatch(r'\d+\.\d+(\.\d+)?', data.get('min_version', '')):
        raise ValidationError('Provide a reference and minimum version, such as 1.8.1')
    if data.get('commit') and not re.fullmatch('[a-fA-F0-9]{40}', data['commit']):
        raise ValidationError('Pinned commit must be a full 40-character SHA')
    if data.get('protocol') != 'fastchess-ob':
        raise ValidationError('Unsupported runner protocol')


def verify_variant_config(data):
    if not re.fullmatch('[a-zA-Z0-9_-]+', data.get('fastchess_variant', '')) or type(data.get('syzygy')) is not bool:
        raise ValidationError('Provide a Fastchess variant name and Syzygy support flag')

OPENBENCH_STATIC_VERSION = 'mattbench-red-4'
OPENBENCH_CONFIG = ConfigMapping()
OPENBENCH_CUSTOM_FOCUS = True


def verify_engine_test_preset(test_preset):

    valid_keys = [

        'both_branch',
        'both_bench',
        'both_network',
        'both_options',
        'both_time_control',

        'dev_branch',
        'dev_bench',
        'dev_network',
        'dev_options',
        'dev_time_control',

        'base_branch',
        'base_bench',
        'base_network',
        'base_options',
        'base_time_control',

        'test_bounds',
        'test_confidence',
        'test_max_games',

        'book_name',
        'upload_pgns',
        'priority',
        'throughput',
        'workload_size',
        'syzygy_wdl',

        'syzygy_adj',
        'win_adj',
        'draw_adj',
    ]

    valid_keys += ['test_mode', 'dev_repo', 'base_repo', 'base_engine', 'scale_method', 'scale_nps', 'info', 'variant']

    for key in test_preset.keys():
        if key not in valid_keys:
            raise Exception('Contains invalid key: %s' % (key))
    return valid_keys

def verify_engine_tune_preset(tune_preset):

    valid_keys = [

        'both_branch',
        'both_bench',
        'both_network',
        'both_options',
        'both_time_control',

        'dev_branch',
        'dev_bench',
        'dev_network',
        'dev_options',
        'dev_time_control',

        'spsa_reporting_type',
        'spsa_distribution_type',
        'spsa_alpha',
        'spsa_gamma',
        'spsa_A_ratio',
        'spsa_iterations',
        'spsa_pairs_per',

        'book_name',
        'upload_pgns',
        'priority',
        'throughput',
        'syzygy_wdl',

        'syzygy_adj',
        'win_adj',
        'draw_adj',
    ]

    valid_keys += ['dev_repo', 'scale_method', 'scale_nps', 'spsa_inputs', 'info', 'variant']

    for key in tune_preset.keys():
        if key not in valid_keys:
            raise Exception('Contains invalid key: %s' % (key))
    return valid_keys

def verify_engine_datagen_preset(datagen_preset):

    valid_keys = [

        'both_branch',
        'both_bench',
        'both_network',
        'both_options',
        'both_time_control',

        'dev_branch',
        'dev_bench',
        'dev_network',
        'dev_options',
        'dev_time_control',

        'base_branch',
        'base_bench',
        'base_network',
        'base_options',
        'base_time_control',

        'book_name',
        'upload_pgns',
        'priority',
        'throughput',
        'workload_size',
        'syzygy_wdl',

        'syzygy_adj',
        'win_adj',
        'draw_adj',

        'datagen_custom_genfens',
        'datagen_play_reverses',
        'datagen_max_games',
    ]

    valid_keys += ['dev_repo', 'base_repo', 'base_engine', 'scale_method', 'scale_nps', 'info', 'variant']

    for key in datagen_preset.keys():
        if key not in valid_keys:
            raise Exception('Contains invalid key: %s' % (key))
    return valid_keys
