import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone


TRAINING_ACTIVE = ('DOWNLOADING', 'CONVERTING', 'COMPILING', 'TRAINING', 'SAVING')
TRAINING_TERMINAL = ('COMPLETED', 'FAILED', 'CANCELLED')
TRAINING_STATES = (
    ('VALIDATING', 'Checking inputs'), ('PREPARING', 'Checking inputs'),
    ('QUEUED', 'Waiting for worker'), ('DOWNLOADING', 'Downloading'),
    ('CONVERTING', 'Converting'), ('COMPILING', 'Compiling'),
    ('TRAINING', 'Training'), ('SAVING', 'Saving outputs'),
    ('COMPLETED', 'Completed'), ('FAILED', 'Failed'), ('CANCELLED', 'Cancelled'),
)


class HuggingFaceCredential(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='huggingface')
    account = models.CharField(max_length=128)
    wrapped_key = models.TextField()
    ciphertext = models.TextField()
    updated = models.DateTimeField(auto_now=True)


class TrainingSchedule(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    engine = models.ForeignKey('OpenBench.EngineConfig', on_delete=models.PROTECT, related_name='training_schedules', null=True, blank=True)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, null=True, blank=True)
    name = models.CharField(max_length=128)
    files = models.JSONField(default=dict)
    settings = models.JSONField(default=dict)
    version = models.PositiveIntegerField(default=1)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['engine__name', 'name']
        constraints = [
            models.UniqueConstraint(fields=['engine', 'name'], condition=models.Q(owner__isnull=True), name='unique_engine_training_schedule'),
            models.UniqueConstraint(fields=['engine', 'owner', 'name'], condition=models.Q(owner__isnull=False), name='unique_user_training_schedule'),
            models.UniqueConstraint(fields=['name'], condition=models.Q(engine__isnull=True, owner__isnull=True), name='unique_global_training_schedule'),
            models.UniqueConstraint(fields=['owner', 'name'], condition=models.Q(engine__isnull=True, owner__isnull=False), name='unique_personal_training_schedule'),
        ]

    @property
    def scope(self):
        return 'personal' if self.owner_id else 'engine' if self.engine_id else 'global'

    @property
    def scope_label(self):
        return self.scope.capitalize()


class TrainingDataset(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='training_datasets')
    name = models.CharField(max_length=128)
    repo = models.CharField(max_length=256)
    revision = models.CharField(max_length=200, default='main')
    patterns = models.TextField(default='*')
    notes = models.TextField(blank=True)
    metadata = models.JSONField(default=dict)
    checked = models.DateTimeField(null=True, blank=True)
    archived = models.BooleanField(default=False)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']
        constraints = [models.UniqueConstraint(fields=['owner', 'name'], name='unique_user_training_dataset')]

    def snapshot(self):
        return {'registry_id': str(self.pk), 'name': self.name, 'repo': self.repo, 'ref': self.revision, 'patterns': [line.strip() for line in self.patterns.splitlines() if line.strip()]}


class TrainingWorker(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='training_workers')
    name = models.CharField(max_length=128)
    secret_hash = models.CharField(max_length=64)
    info = models.JSONField(default=dict)
    updated = models.DateTimeField(default=timezone.now, db_index=True)
    enabled = models.BooleanField(default=True)


class TrainingRun(models.Model):
    claim_id = models.UUIDField(null=True, blank=True, unique=True)
    recovery_run = models.OneToOneField('self', on_delete=models.PROTECT, null=True, blank=True, related_name='recovered_from')
    preparation = models.JSONField(default=dict)
    task_attempts = models.PositiveIntegerField(default=0)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='training_runs')
    engine = models.ForeignKey('OpenBench.EngineConfig', on_delete=models.PROTECT)
    name = models.CharField(max_length=128)
    schedule = models.ForeignKey(TrainingSchedule, on_delete=models.SET_NULL, null=True)
    snapshot = models.JSONField(default=dict)
    dataset = models.JSONField(default=dict)
    parameters = models.JSONField(default=dict)
    worker = models.ForeignKey(TrainingWorker, on_delete=models.PROTECT, null=True, blank=True, related_name='runs')
    requested_worker = models.ForeignKey(TrainingWorker, on_delete=models.SET_NULL, null=True, blank=True, related_name='requested_runs')
    state = models.CharField(max_length=16, choices=TRAINING_STATES, default='VALIDATING', db_index=True)
    created = models.DateTimeField(default=timezone.now)
    updated = models.DateTimeField(default=timezone.now, db_index=True)
    started = models.DateTimeField(null=True, blank=True)
    finished = models.DateTimeField(null=True, blank=True)
    metrics = models.JSONField(default=dict)
    history = models.JSONField(default=list)
    log_tail = models.TextField(blank=True)
    error = models.TextField(blank=True)
    report_sequence = models.BigIntegerField(default=0)
    cancel_requested = models.BooleanField(default=False)
    resume_from = models.ForeignKey('TrainingCheckpoint', on_delete=models.PROTECT, null=True, blank=True, related_name='resumed_runs')

    class Meta:
        ordering = ['-created']
        constraints = [
            models.UniqueConstraint(fields=['worker'], condition=models.Q(state__in=TRAINING_ACTIVE), name='one_training_per_worker'),
        ]

    @property
    def terminal(self):
        return self.state in TRAINING_TERMINAL

    @property
    def dataset_url(self):
        return 'https://huggingface.co/datasets/' + self.dataset.get('repo', '')


class TrainingArtifact(models.Model):
    run = models.ForeignKey(TrainingRun, on_delete=models.CASCADE, related_name='artifacts')
    name = models.CharField(max_length=128)
    sha256 = models.CharField(max_length=64)
    size = models.BigIntegerField()
    path = models.CharField(max_length=256)
    kind = models.CharField(max_length=16, default='network')
    network = models.ForeignKey('OpenBench.Network', on_delete=models.SET_NULL, null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['run', 'name'], name='unique_training_artifact')]


class DatasetUpload(models.Model):
    task_attempts = models.PositiveIntegerField(default=0)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    workload = models.ForeignKey('OpenBench.Test', on_delete=models.PROTECT, related_name='dataset_uploads')
    repo = models.CharField(max_length=256)
    filename = models.CharField(max_length=512)
    private = models.BooleanField(default=True)
    state = models.CharField(max_length=16, default='QUEUED', db_index=True)
    progress = models.FloatField(default=0)
    stage = models.CharField(max_length=64, default='Waiting to upload')
    revision = models.CharField(max_length=40, blank=True)
    sha256 = models.CharField(max_length=64, blank=True)
    error = models.TextField(blank=True)
    created = models.DateTimeField(default=timezone.now)
    updated = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-created']
        constraints = [
            models.UniqueConstraint(fields=['repo', 'filename'], condition=models.Q(state__in=['QUEUED', 'UPLOADING']), name='one_dataset_upload_per_path'),
        ]

    @property
    def url(self):
        from urllib.parse import quote
        return 'https://huggingface.co/datasets/%s/blob/%s/%s' % (self.repo, self.revision or 'main', quote(self.filename))


class TrainingCheckpoint(models.Model):
    run = models.ForeignKey(TrainingRun, on_delete=models.CASCADE, related_name='checkpoints')
    superbatch = models.PositiveIntegerField()
    archive = models.OneToOneField(TrainingArtifact, on_delete=models.PROTECT, related_name='checkpoint_archive')
    network = models.OneToOneField(TrainingArtifact, on_delete=models.PROTECT, related_name='checkpoint_network')
    metadata = models.JSONField(default=dict)
    created = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-superbatch']
        constraints = [models.UniqueConstraint(fields=['run', 'superbatch'], name='unique_training_checkpoint')]


class LifecycleEvent(models.Model):
    event_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    key = models.CharField(max_length=200, unique=True)
    kind = models.CharField(max_length=64, db_index=True)
    subject_type = models.CharField(max_length=32)
    subject_id = models.CharField(max_length=64)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    data = models.JSONField(default=dict)
    version = models.PositiveIntegerField(default=1)
    created = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['id']


class TrainingServiceLease(models.Model):
    name = models.CharField(max_length=64, primary_key=True)
    token = models.UUIDField()
    expires = models.DateTimeField()
