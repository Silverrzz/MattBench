import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone

from OpenBench.notification_events import default_events


class DiscordConfiguration(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    enabled = models.BooleanField(default=False)
    webhook_ciphertext = models.TextField(blank=True)
    generation = models.UUIDField(default=uuid.uuid4)
    destination_label = models.CharField(max_length=128, blank=True)
    guild_id = models.CharField(max_length=20, blank=True)
    channel_id = models.CharField(max_length=20, blank=True)
    canonical_url = models.URLField(blank=True)
    sender_seen = models.DateTimeField(null=True, blank=True)
    last_success = models.DateTimeField(null=True, blank=True)
    next_send_at = models.DateTimeField(default=timezone.now)
    lease_token = models.UUIDField(null=True, blank=True)
    lease_until = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.CheckConstraint(condition=models.Q(id=1), name='discord_singleton')]

    @property
    def channel_url(self):
        return 'https://discord.com/channels/%s/%s' % (self.guild_id, self.channel_id) if self.guild_id and self.channel_id else ''


class NotificationPreferences(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='notifications')
    enabled = models.BooleanField(default=False)
    discord_user_id = models.CharField(max_length=20, blank=True)
    events = models.JSONField(default=default_events)


class NotificationDelivery(models.Model):
    event = models.ForeignKey('OpenBench.LifecycleEvent', on_delete=models.CASCADE, null=True, blank=True)
    recipient = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    event_key = models.CharField(max_length=64)
    subject_key = models.CharField(max_length=100)
    generation = models.UUIDField()
    summary = models.JSONField(default=dict)
    ping_requested = models.BooleanField(default=False)
    status = models.CharField(max_length=16, default='pending', db_index=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    next_attempt_at = models.DateTimeField(default=timezone.now)
    created = models.DateTimeField(default=timezone.now)
    claimed_until = models.DateTimeField(null=True, blank=True)
    claim_token = models.UUIDField(null=True, blank=True)
    message_id = models.CharField(max_length=20, blank=True)
    last_error = models.CharField(max_length=200, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['event', 'recipient'], name='unique_discord_event_recipient')]
        indexes = [models.Index(fields=['subject_key', 'id'], name='discord_subject_order')]
