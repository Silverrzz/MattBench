import copy, json

from pathlib import Path
from django.core.exceptions import ValidationError
from django.db import transaction
from OpenBench.config import fingerprint, read_site_config, verify_engine_config, verify_book_config, verify_preset
from OpenBench.models import EngineConfig, OpeningBook, WorkloadPreset, Runner, RunnerRelease, Variant


def import_variant_defaults():

    for name, data in read_site_config()['variants'].items():
        if Variant.objects.filter(name=name).exists():
            continue
        source = data['runner']['repo_url']
        runner, _ = Runner.objects.get_or_create(name='import-' + fingerprint(source)[:16],
                                                defaults={'enabled': True, 'settings': {'source': source}})
        release_data = {'ref': data['runner']['repo_ref'], 'min_version': data['runner']['min_version'], 'protocol': 'fastchess-ob'}
        release, _ = RunnerRelease.objects.get_or_create(name='import-' + fingerprint([source, release_data])[:16],
                         defaults={'enabled': True, 'runner': runner, 'settings': release_data})
        Variant.objects.create(name=name, enabled=True, runner_release=release,
                               settings={key: data[key] for key in ('fastchess_variant', 'syzygy')})


def read_directory(directory, kind):

    root = Path(directory).resolve()
    if not root.is_dir():
        raise ValidationError('Directory does not exist: %s' % root)
    entries = {}
    for path in sorted(root.glob('*.json')):
        if not path.resolve().is_relative_to(root):
            raise ValidationError('Configuration path escapes its directory: %s' % path.name)
        try:
            with path.open(encoding='utf-8-sig') as stream:
                data = json.load(stream)
        except (OSError, ValueError) as error:
            raise ValidationError('%s: %s' % (path.name, error)) from error
        name = path.stem
        if not name or len(name) > 128 or not isinstance(data, dict):
            raise ValidationError('Invalid configuration: %s' % path.name)
        presets = {}
        if kind == 'engines':
            for field, workload in (('test_presets', 'TEST'), ('tune_presets', 'TUNE'), ('datagen_presets', 'DATAGEN')):
                values = data.pop(field, {'default': {}})
                if not isinstance(values, dict) or not isinstance(values.get('default', {}), dict):
                    raise ValidationError('Invalid %s in %s' % (field, path.name))
                presets[workload] = {}
                for label, value in values.items():
                    if not label.strip() or len(label) > 128 or not isinstance(value, dict):
                        raise ValidationError('Invalid preset in %s' % path.name)
                    resolved = values.get('default', {}) | value
                    verify_preset(workload, resolved)
                    presets[workload][label] = resolved
            try:
                verify_engine_config(data)
            except ValidationError as error:
                raise ValidationError('%s: %s' % (path.name, '; '.join(error.messages))) from error
        else:
            data.pop('format', None)
            try:
                verify_book_config(name, data)
            except ValidationError as error:
                raise ValidationError('%s: %s' % (path.name, '; '.join(error.messages))) from error
        entries[name] = (data, presets)
    if not entries:
        raise ValidationError('No JSON configuration files found in %s' % root)
    return entries


@transaction.atomic
def import_directory(directory, kind, apply=False, replace=False, disable_missing=False):

    entries = read_directory(directory, kind)
    model = EngineConfig if kind == 'engines' else OpeningBook
    existing = set(model.objects.filter(name__in=entries).values_list('name', flat=True))
    missing = model.objects.exclude(name__in=entries).filter(enabled=True)
    result = {'new': len(entries) - len(existing), 'updated': len(existing) if replace else 0,
              'skipped': 0 if replace else len(existing), 'disabled': missing.count() if disable_missing else 0}
    if not apply:
        return result
    import_variant_defaults()
    for name, (data, presets) in entries.items():
        row = model.objects.select_for_update().filter(name=name).first()
        if row and not replace:
            continue
        row = row or model(name=name)
        row.settings, row.enabled = copy.deepcopy(data), True
        if kind == 'engines':
            names = data.get('variants', ['standard', 'fischerandom'])
        else:
            names = [data.get('variant', 'fischerandom' if any(marker in name.upper() for marker in ('FRC', '960', 'FISCHER')) else 'standard')]
        variants = list(Variant.objects.filter(name__in=names, enabled=True, runner_release__enabled=True, runner_release__runner__enabled=True))
        if not names or len(variants) != len(set(names)):
            raise ValidationError('Unknown or disabled variants for %s' % name)
        if kind == 'books':
            row.variant = variants[0]
        row.full_clean()
        row.save()
        if kind == 'engines':
            row.variants.set(variants)
        for workload, values in presets.items():
            row.presets.filter(owner=None, workload_type=workload).exclude(name__in=values).delete()
            for position, (label, value) in enumerate(values.items()):
                preset = WorkloadPreset.objects.filter(engine=row, owner=None, workload_type=workload, name=label).first()
                preset = preset or WorkloadPreset(engine=row, workload_type=workload, name=label)
                preset.settings, preset.position = value, position
                preset.full_clean()
                preset.save()
    if disable_missing:
        missing.update(enabled=False)
    return result
