import copy
import hashlib
import json
import os
from collections.abc import Mapping
from contextvars import ContextVar

from cryptography.fernet import Fernet
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from OpenBench.configuration_schema import DEFAULT_SITE


_request_snapshot = ContextVar('configuration_snapshot', default=None)
_unspecified_generation = object()


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def empty_snapshot():
    return dict(DEFAULT_SITE, engines={}, books={}, variants={})


def current_snapshot():
    from OpenBench.models import ConfigurationRevision
    if (snapshot := _request_snapshot.get()) is not None:
        return snapshot
    revision = ConfigurationRevision.objects.order_by('-generation').first()
    return revision.snapshot if revision else empty_snapshot()


class ConfigMapping(Mapping):
    def __getitem__(self, key):
        return current_snapshot()[key]

    def __iter__(self):
        return iter(current_snapshot())

    def __len__(self):
        return len(current_snapshot())


class ConfigurationMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        token = _request_snapshot.set(current_snapshot())
        try:
            return self.get_response(request)
        finally:
            _request_snapshot.reset(token)


def eligibility_fingerprint(snapshot=None):
    snapshot = snapshot if snapshot is not None else current_snapshot()
    return fingerprint({name: {'build': data['build'], 'private': data['private'], 'source': data['source'], 'variants': data.get('variants', [])} for name, data in snapshot['engines'].items()})


def can_manage_engine(actor, engine):
    return bool(actor and actor.is_active and (actor.is_superuser or engine.maintainers.filter(user=actor).exists()))


def authorize(instance, actor):
    from OpenBench.models import EngineConfig, WorkloadPreset, SiteSettings, Runner, RunnerRelease, Variant, OpeningBook, EngineMaintainer
    if type(instance) not in (EngineConfig, WorkloadPreset, SiteSettings, Runner, RunnerRelease, Variant, OpeningBook, EngineMaintainer):
        raise PermissionDenied('Unsupported configuration entity')
    if not actor or not actor.is_active:
        raise PermissionDenied('An active configuration editor is required')
    if actor.is_superuser:
        return
    if isinstance(instance, EngineConfig) and not instance._state.adding and can_manage_engine(actor, instance):
        return
    if isinstance(instance, WorkloadPreset):
        if instance.owner_id == actor.pk or (instance.owner_id is None and can_manage_engine(actor, instance.engine)):
            return
    raise PermissionDenied('You cannot manage this configuration')


def compile_snapshot(site):
    from OpenBench.models import EngineConfig, OpeningBook, Variant
    snapshot = copy.deepcopy(site.settings)
    snapshot.update(engines={}, books={}, variants={})
    for variant in Variant.objects.filter(enabled=True).select_related('runner_release__runner'):
        release = variant.runner_release
        if not release.enabled or not release.runner.enabled:
            raise ValidationError('Variant %s requires an enabled runner release' % variant.name)
        snapshot['variants'][variant.name] = dict(variant.settings, runner={
            'repo_url': release.runner.settings['source'],
            'repo_ref': release.settings.get('commit', release.settings['ref']),
            'min_version': release.settings['min_version'],
        })
    for book in OpeningBook.objects.filter(enabled=True).select_related('variant'):
        if book.variant.name not in snapshot['variants']:
            raise ValidationError('Book %s requires an enabled variant' % book.name)
        snapshot['books'][book.name] = dict(book.settings, variant=book.variant.name)
    for engine in EngineConfig.objects.filter(enabled=True).prefetch_related('variants', 'presets'):
        settings = copy.deepcopy(engine.settings)
        variants = sorted(variant.name for variant in engine.variants.all())
        if not variants or any(variant not in snapshot['variants'] for variant in variants):
            raise ValidationError('Engine %s requires enabled variants' % engine.name)
        if settings['nps'] <= 0 or not settings['source'].startswith('https://') or not settings['build']['systems']:
            raise ValidationError('Engine %s is incomplete; save it as a draft' % engine.name)
        if not settings['private'] and not settings['build']['compilers']:
            raise ValidationError('Engine %s requires a compiler' % engine.name)
        settings['variants'] = variants
        for kind in ('test_presets', 'tune_presets', 'datagen_presets'):
            settings[kind] = {'default': {}}
        for preset in engine.presets.all():
            if preset.owner_id is None:
                kind = {'TEST': 'test_presets', 'TUNE': 'tune_presets', 'DATAGEN': 'datagen_presets'}[preset.workload_type]
                settings[kind][preset.name] = preset.settings
        snapshot['engines'][engine.name] = settings
    return snapshot


def publish(site, actor, summary):
    from OpenBench.models import ConfigurationRevision
    snapshot = compile_snapshot(site)
    site.generation += 1
    site.save()
    return ConfigurationRevision.objects.create(generation=site.generation, actor=actor, summary=summary,
        snapshot=snapshot, fingerprint=fingerprint(snapshot), eligibility_fingerprint=eligibility_fingerprint(snapshot))


def lock_settings(expected_generation=_unspecified_generation):
    from OpenBench.models import SiteSettings
    SiteSettings.objects.get_or_create(pk=1, defaults={'settings': DEFAULT_SITE})
    site = SiteSettings.objects.select_for_update().get(pk=1)
    if expected_generation is not _unspecified_generation and site.generation != expected_generation:
        raise ValidationError('Configuration changed; reload before saving')
    return site


@transaction.atomic
def save_configuration(instance, actor, expected_generation, variants=None):
    from OpenBench.models import SiteSettings, EngineConfig
    site = lock_settings(expected_generation)
    authorize(instance, actor)
    if not instance._state.adding:
        original = type(instance).objects.get(pk=instance.pk)
        authorize(original, actor)
        if isinstance(instance, EngineConfig) and original.name != instance.name:
            raise ValidationError('Engine identifiers cannot be renamed while legacy workloads reference their names')
    instance.full_clean()
    instance.save()
    if variants is not None:
        if not isinstance(instance, EngineConfig):
            raise ValidationError('Only engines declare supported variants')
        instance.variants.set(variants)
    if isinstance(instance, SiteSettings):
        site.settings = instance.settings
    return publish(site, actor, 'Updated %s %s' % (instance._meta.model_name, instance.pk))


@transaction.atomic
def delete_configuration(instance, actor, expected_generation):
    from OpenBench.models import WorkloadPreset, EngineMaintainer
    if type(instance) not in (WorkloadPreset, EngineMaintainer):
        raise ValidationError('Archive configuration entities by disabling them')
    site = lock_settings(expected_generation)
    original = type(instance).objects.get(pk=instance.pk)
    authorize(original, actor)
    summary = 'Deleted %s %s' % (original._meta.model_name, original.pk)
    original.delete()
    return publish(site, actor, summary)


@transaction.atomic
def set_credential(engine, token, actor, expected_generation):
    from OpenBench.models import Credential
    if not actor or not actor.is_active or not actor.is_superuser:
        raise PermissionDenied('Only administrators manage repository credentials')
    site = lock_settings(expected_generation)
    encrypted = Fernet(os.environ['OPENBENCH_CREDENTIAL_KEY'].encode()).encrypt(token.encode()).decode()
    Credential.objects.update_or_create(engine=engine, defaults={'ciphertext': encrypted})
    return publish(site, actor, 'Updated credential for %s' % engine.pk)


def read_credential(engine_name):
    from OpenBench.models import Credential
    credential = Credential.objects.filter(engine__name=engine_name, engine__enabled=True).first()
    if credential:
        return Fernet(os.environ['OPENBENCH_CREDENTIAL_KEY'].encode()).decrypt(credential.ciphertext.encode()).decode()
