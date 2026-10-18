import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone


class TrainingProfile(models.Model):
    id = models.SlugField(primary_key=True, max_length=64)
    name = models.CharField(max_length=128)
    version = models.PositiveIntegerField(default=1)
    enabled = models.BooleanField(default=True)
    settings = models.JSONField(default=dict)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']


class Workflow(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    name = models.CharField(max_length=128)
    version = models.PositiveIntegerField(default=1)
    definition = models.JSONField(default=dict)
    updated = models.DateTimeField(auto_now=True)
    archived = models.BooleanField(default=False)

    class Meta:
        ordering = ['-updated']


class WorkflowRun(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workflow = models.ForeignKey(Workflow, on_delete=models.PROTECT, related_name='runs')
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    version = models.PositiveIntegerField()
    snapshot = models.JSONField(default=dict)
    state = models.CharField(max_length=16, default='RUNNING', db_index=True)
    message = models.TextField(blank=True)
    created = models.DateTimeField(default=timezone.now)
    updated = models.DateTimeField(default=timezone.now)
    finished = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created']


class WorkflowStep(models.Model):
    run = models.ForeignKey(WorkflowRun, on_delete=models.CASCADE, related_name='steps')
    key = models.SlugField(max_length=64)
    state = models.CharField(max_length=16, default='PENDING', db_index=True)
    outcome = models.CharField(max_length=16, blank=True)
    attempts = models.PositiveIntegerField(default=0)
    history = models.JSONField(default=list)
    outputs = models.JSONField(default=dict)
    message = models.TextField(blank=True)
    test = models.ForeignKey('OpenBench.Test', on_delete=models.PROTECT, null=True, blank=True)
    training = models.ForeignKey('OpenBench.TrainingRun', on_delete=models.PROTECT, null=True, blank=True)
    upload = models.ForeignKey('OpenBench.DatasetUpload', on_delete=models.PROTECT, null=True, blank=True)
    started = models.DateTimeField(null=True, blank=True)
    updated = models.DateTimeField(default=timezone.now)
    finished = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['id']
        constraints = [models.UniqueConstraint(fields=['run', 'key'], name='unique_workflow_step')]
