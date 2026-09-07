import copy
import json
import re
from pathlib import Path

from django.core.exceptions import ValidationError
from django.db import transaction

from OpenBench.configuration import publish, lock_settings
from OpenBench.configuration_schema import DEFAULT_SITE, validate_settings, validate_preset


def read_legacy_config(directory):
    root = Path(directory).resolve()

    def read(folder, name):
        path = (root / folder / name).resolve()
        if not path.is_relative_to(root / folder):
            raise ValidationError('Configuration path escapes its directory')
        with path.open(encoding='utf-8-sig') as stream:
            return json.load(stream)

    original = read('Config', 'config.json')
    unknown = set(original) - set(DEFAULT_SITE) - {'books', 'engines', 'variants'}
    if unknown:
        raise ValidationError('Unknown site settings: %s' % ', '.join(sorted(unknown)))
    site = DEFAULT_SITE | {key: value for key, value in original.items() if key in DEFAULT_SITE}
    validate_settings('sitesettings', site)
    result = {'site': site, 'engines': {}, 'books': {}, 'variants': {}}
    variants = original.get('variants', {'standard': {}, 'fischerandom': {'syzygy': True}})
    for name, definition in variants.items():
        if not re.fullmatch(r'[a-z][a-z0-9_-]*', name):
            raise ValidationError('Invalid variant identifier: %s' % name)
        settings = {'fastchess_variant': definition.get('fastchess_variant', name), 'syzygy': definition.get('syzygy', name == 'standard')}
        runner = definition.get('runner', {key: site['fastchess_' + key] for key in ('repo_url', 'repo_ref', 'min_version')})
        validate_settings('variant', settings)
        validate_settings('runner', {'source': runner['repo_url']})
        release = {'ref': runner['repo_ref'], 'min_version': runner['min_version'], 'protocol': 'fastchess-ob'}
        if re.fullmatch('[0-9a-f]{40}', runner['repo_ref']):
            release['commit'] = runner['repo_ref']
        validate_settings('runnerrelease', release)
        result['variants'][name] = {'settings': settings, 'runner': runner['repo_url'], 'release': release}
    for name in original['books']:
        book = read('Books', name + '.json')
        variant = book.pop('variant', 'fischerandom' if any(marker in name.upper() for marker in ('FRC', '960', 'FISCHER')) else 'standard')
        book.setdefault('format', name.rsplit('.', 1)[-1].lower())
        validate_settings('openingbook', book)
        if variant not in variants:
            raise ValidationError('Unknown variant for book %s' % name)
        result['books'][name] = {'settings': book, 'variant': variant}
    for name in original['engines']:
        engine = read('Engines', name + '.json')
        supported = engine.pop('variants', ['standard', 'fischerandom'])
        if not isinstance(supported, list) or not supported or any(variant not in variants for variant in supported):
            raise ValidationError('Unknown or missing variants for engine %s' % name)
        presets = {}
        for field, kind in (('test_presets', 'TEST'), ('tune_presets', 'TUNE'), ('datagen_presets', 'DATAGEN')):
            legacy = engine.pop(field, {'default': {}})
            defaults = legacy.get('default', {})
            presets[kind] = {}
            for preset, settings in legacy.items():
                resolved = defaults | settings
                validate_preset(kind, resolved)
                presets[kind][preset] = resolved
        validate_settings('engineconfig', engine)
        if engine['nps'] <= 0 or not engine['source'].startswith('https://'):
            raise ValidationError('Engine %s is not ready to enable' % name)
        result['engines'][name] = {'settings': engine, 'variants': supported, 'presets': presets}
    return result


@transaction.atomic
def import_legacy_config(bundle, replace=False, actor=None):
    from OpenBench.models import Runner, RunnerRelease, Variant, EngineConfig, OpeningBook, WorkloadPreset, ConfigurationRevision
    site = lock_settings()
    conflicts = list(EngineConfig.objects.filter(name__in=bundle['engines']).values_list('name', flat=True))
    conflicts += list(OpeningBook.objects.filter(name__in=bundle['books']).values_list('name', flat=True))
    latest = ConfigurationRevision.objects.order_by('-generation').first()
    pristine = latest is None
    if not pristine:
        if site.settings != bundle['site']:
            conflicts.append('site settings')
        for variant in Variant.objects.filter(name__in=bundle['variants']).select_related('runner_release__runner'):
            incoming = bundle['variants'][variant.name]
            if (variant.settings, variant.runner_release.settings, variant.runner_release.runner.settings['source']) != (incoming['settings'], incoming['release'], incoming['runner']):
                conflicts.append('variant ' + variant.name)
    if conflicts and not replace:
        raise ValidationError('Existing configuration requires --replace: %s' % ', '.join(conflicts))

    def save(model, name, **fields):
        instance = model.objects.filter(name=name).first() or model(name=name)
        for key, value in fields.items():
            setattr(instance, key, copy.deepcopy(value))
        instance.full_clean()
        instance.save()
        return instance

    site.settings = bundle['site']
    site.full_clean()
    variants = {}
    from OpenBench.configuration import fingerprint
    for name, data in bundle['variants'].items():
        runner = save(Runner, 'import-' + fingerprint(data['runner'])[:16], enabled=True, settings={'source': data['runner']})
        release = save(RunnerRelease, 'import-' + fingerprint([data['runner'], data['release']])[:16], enabled=True, runner=runner, settings=data['release'])
        variants[name] = save(Variant, name, enabled=True, runner_release=release, settings=data['settings'])
    for name, data in bundle['books'].items():
        save(OpeningBook, name, enabled=True, variant=variants[data['variant']], settings=data['settings'])
    for name, data in bundle['engines'].items():
        engine = save(EngineConfig, name, enabled=True, settings=data['settings'])
        engine.variants.set([variants[variant] for variant in data['variants']])
        for kind, presets in data['presets'].items():
            if replace:
                engine.presets.filter(owner=None, workload_type=kind).exclude(name__in=presets).delete()
            for position, (name, settings) in enumerate(presets.items()):
                preset = WorkloadPreset.objects.filter(engine=engine, owner=None, workload_type=kind, name=name).first()
                preset = preset or WorkloadPreset(engine=engine, workload_type=kind, name=name)
                preset.settings, preset.position = settings, position
                preset.full_clean()
                preset.save()
    return publish(site, actor, 'Imported legacy configuration')
