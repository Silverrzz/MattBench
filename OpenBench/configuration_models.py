import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models


class ConfigEntity(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=128, unique=True)
    enabled = models.BooleanField(default=False)
    schema_version = models.PositiveIntegerField(default=1)
    settings = models.JSONField(default=dict, blank=True)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True

    def clean(self):
        from OpenBench.configuration_schema import validate_settings
        validate_settings(self._meta.model_name, self.settings, self.schema_version)

    def __str__(self):
        return self.name


class SiteSettings(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    settings = models.JSONField(default=dict, blank=True)
    schema_version = models.PositiveIntegerField(default=1)
    generation = models.PositiveBigIntegerField(default=0)

    def clean(self):
        from OpenBench.configuration_schema import validate_settings
        if self.pk != 1:
            raise ValidationError('Only one site settings record is allowed')
        validate_settings('sitesettings', self.settings, self.schema_version)


class Runner(ConfigEntity):
    def __str__(self):
        return self.settings.get('source', '').removeprefix('https://github.com/').rstrip('/') if self.name.startswith('import-') else self.name


class RunnerRelease(ConfigEntity):
    runner = models.ForeignKey(Runner, on_delete=models.PROTECT, related_name='releases')

    def __str__(self):
        return '%s / %s' % (self.runner, self.settings.get('ref', '')[:12]) if self.name.startswith('import-') else self.name

    def save(self, *args, **kwargs):
        if not self._state.adding:
            original = type(self).objects.get(pk=self.pk)
            if (original.runner_id, original.settings, original.schema_version) != (self.runner_id, self.settings, self.schema_version):
                raise ValidationError('Create a new runner release to change execution settings')
        return super().save(*args, **kwargs)


class Variant(ConfigEntity):
    runner_release = models.ForeignKey(RunnerRelease, on_delete=models.PROTECT)


class EngineConfig(ConfigEntity):
    variants = models.ManyToManyField(Variant, related_name='engines')


class EngineMaintainer(models.Model):
    engine = models.ForeignKey(EngineConfig, on_delete=models.CASCADE, related_name='maintainers')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['engine', 'user'], name='unique_engine_maintainer')]


class OpeningBook(ConfigEntity):
    variant = models.ForeignKey(Variant, on_delete=models.PROTECT)


class WorkloadPreset(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    engine = models.ForeignKey(EngineConfig, on_delete=models.PROTECT, related_name='presets')
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.CASCADE)
    name = models.CharField(max_length=128)
    workload_type = models.CharField(max_length=8, choices=[('TEST', 'Test'), ('TUNE', 'Tune'), ('DATAGEN', 'Datagen')])
    position = models.PositiveIntegerField(default=0)
    schema_version = models.PositiveIntegerField(default=1)
    settings = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['position', 'name', 'id']
        constraints = [
            models.UniqueConstraint(fields=['engine', 'workload_type', 'name'], condition=models.Q(owner__isnull=True), name='unique_shared_preset'),
            models.UniqueConstraint(fields=['engine', 'owner', 'workload_type', 'name'], condition=models.Q(owner__isnull=False), name='unique_personal_preset'),
        ]

    def clean(self):
        from OpenBench.configuration_schema import validate_preset
        validate_preset(self.workload_type, self.settings, self.schema_version)


class ConfigurationRevision(models.Model):
    generation = models.PositiveBigIntegerField(unique=True)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    created = models.DateTimeField(auto_now_add=True)
    summary = models.CharField(max_length=256)
    snapshot = models.JSONField()
    fingerprint = models.CharField(max_length=64)
    eligibility_fingerprint = models.CharField(max_length=64)

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError('Configuration revisions are immutable')
        return super().save(*args, **kwargs)
