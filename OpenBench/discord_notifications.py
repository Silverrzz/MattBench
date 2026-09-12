"""Discord transport and transactional outbox. Never called to send by workers."""
import copy
import logging
import math
import re
import uuid
from datetime import timedelta
from urllib.parse import urlsplit

import requests
from cryptography.fernet import InvalidToken
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.db import transaction
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from OpenBench.models import DiscordConfiguration, NotificationDelivery, NotificationPreferences
from OpenBench.notification_events import EVENTS, normalize

logger = logging.getLogger(__name__)
SNOWFLAKE = re.compile(r'[1-9][0-9]{16,19}')
WEBHOOK = re.compile(r'https://discord\.com/api(?:/v10)?/webhooks/([1-9][0-9]{16,19})/([A-Za-z0-9_-]{30,200})')
ACTIVE = ('pending', 'sending')


def canonical_webhook(url):
    if not isinstance(url, str) or not (match := WEBHOOK.fullmatch(url)):
        raise ValidationError('Use a canonical https://discord.com/api/webhooks/ID/TOKEN URL without query parameters.')
    return 'https://discord.com/api/webhooks/%s/%s' % match.groups()


def canonical_site(url):
    URLValidator(schemes=['https'])(url)
    parts = urlsplit(url)
    if parts.username or parts.password or parts.query or parts.fragment or parts.path not in ('', '/'):
        raise ValidationError('Use the canonical HTTPS Mattbench origin, without a path, query or credentials.')
    return url.rstrip('/')


def encrypt_webhook(url):
    from OpenBench.training import vault
    return vault().encrypt(canonical_webhook(url).encode()).decode()


def decrypt_webhook(config):
    from OpenBench.training import vault
    try:
        return canonical_webhook(vault().decrypt(config.webhook_ciphertext.encode()).decode())
    except (InvalidToken, UnicodeError, ValidationError):
        raise ValidationError('Discord webhook cannot be decrypted. Restore the credential key or replace the webhook.') from None


def validate_webhook(url):
    url = canonical_webhook(url)
    try:
        response = requests.get(url, timeout=(5, 15), allow_redirects=False)
        if response.status_code != 200:
            raise ValidationError('Discord webhook validation failed (HTTP %d).' % response.status_code)
        body = response.json()
        if not isinstance(body, dict) or body.get('type') != 1 or str(body.get('id')) != url.split('/')[-2]:
            raise ValueError
        if not all(SNOWFLAKE.fullmatch(str(body.get(field, ''))) for field in ('guild_id', 'channel_id')):
            raise ValueError
        return str(body['guild_id']), str(body['channel_id'])
    except (requests.RequestException, ValueError, TypeError):
        raise ValidationError('Discord webhook validation failed. Check the URL and server connectivity.') from None


def safe_text(value, limit=180):
    value = re.sub(r'https?://\S+', '[link omitted]', str(value))
    value = ''.join(c for c in value if c.isprintable()).replace('@', '@\u200b')
    return re.sub(r'([\\`*_{}\[\]()<>|~])', r'\\\1', value)[:limit]


def snapshot(event, subject, key, config):
    family, action = key.split('.')
    names = {'test': 'Test', 'tune': 'Tuning', 'datagen': 'Datagen', 'training': 'Training', 'upload': 'Dataset upload', 'worker': 'Worker'}
    label = EVENTS[key].lower()
    if action == 'queued':
        label = 'queued'
    if action == 'disconnected':
        label = 'disconnected'
    success = action in ('passed', 'completed', 'recovered', 'restored', 'resumed')
    failure = action in ('failed', 'execution_error')
    warning = action in ('interrupted', 'stopped', 'cancel_requested', 'cancelled', 'pause_requested', 'disconnected')
    icon, color = ('✅', 0x2ECC71) if success else ('❌', 0xE74C3C) if failure else ('⚠️', 0xF1C40F) if warning else ('ℹ️', 0x3498DB)
    if action == 'pause_requested':
        icon = '⏸'
    title = '%s %s #%s %s' % (icon, names[family], subject.pk, label)
    fields, description = [], []
    path = '/%s/%s/' % (family, subject.pk)
    if family in ('test', 'tune', 'datagen'):
        title += ' — ' + safe_text(subject.info or subject.dev.name)
        description.append('%s · %s · %s games' % (safe_text(subject.dev_engine), subject.test_mode, format(subject.games, ',')))
        if subject.test_mode == 'SPRT':
            fields.append({'name': 'LLR / bounds', 'value': '%.2f / %.2f, %.2f' % (subject.currentllr, subject.lowerllr, subject.upperllr), 'inline': True})
        if family == 'test' and subject.games > 1:
            from OpenBench.stats import Elo
            try:
                lower, elo, upper = Elo(subject.results())
                error = (upper - lower) / 2
                if math.isfinite(elo) and math.isfinite(error):
                    fields.append({'name': 'Elo', 'value': '%+.1f ± %.1f' % (elo, error), 'inline': True})
            except (ValueError, ZeroDivisionError, OverflowError):
                pass
        if family == 'tune' and hasattr(subject, 'spsa_run'):
            tune = subject.spsa_run
            fields.append({'name': 'Iterations', 'value': '%s / %s' % (subject.games // max(1, 2 * tune.pairs_per), tune.iterations), 'inline': True})
        if action == 'execution_error':
            description.append('Execution stopped.' if subject.finished else 'Work remains active; another worker may continue.')
    elif family == 'training':
        title += ' — ' + safe_text(subject.name)
        description.append(safe_text(subject.engine.name))
        checkpoint = subject.checkpoints.order_by('-superbatch').first()
        saved = checkpoint.superbatch if checkpoint else subject.completed_superbatches
        end = subject.metrics.get('end_superbatch')
        if not end:
            try:
                from OpenBench.training_workloads import run_end
                end = run_end(subject)
            except (KeyError, ValueError, TypeError):
                pass
        if end or saved:
            fields.append({'name': 'Last saved superbatch', 'value': '%s%s' % (saved, ' / %s' % end if end else ''), 'inline': True})
        loss = subject.metrics.get('loss')
        if type(loss) in (float, int) and math.isfinite(loss):
            fields.append({'name': 'Loss', 'value': '%.6g' % loss, 'inline': True})
        if action == 'interrupted':
            reason = event.data.get('reason')
            description.append({'worker_disconnected': 'Worker explicitly disconnected.', 'worker_restarted': 'Worker restarted.', 'heartbeat_lost': 'Worker heartbeat lost.'}.get(reason, 'Worker execution interrupted.'))
            description.append('Recovery pending.' if event.data.get('recovery_pending') or subject.metrics.get('recovery_pending') or subject.state == 'QUEUED' else 'View training for recovery details.')
        if action == 'continued' and subject.resume_from_id:
            description.append('From training #%s, superbatch %s.' % (subject.resume_from.run_id, subject.resume_from.superbatch))
        if action in ('failed', 'cancelled'):
            description.append('View training for details.')
    elif family == 'worker':
        title = '%s Worker %s — %s' % (icon, label, safe_text(getattr(subject, 'name', '') or subject.info.get('machine_name', 'Worker')))
        path = '/workers/%s/' % subject.pk
        if action == 'pause_requested':
            description.append('Current work will finish before pausing.')
        elif action == 'resumed':
            description.append('Eligible for assignments again.')
    elif family == 'upload':
        path = '/datagen/%s/huggingface/' % subject.workload_id
        description.append('Datagen #%s · %s' % (subject.workload_id, 'Published successfully.' if action == 'completed' else 'Upload failed; no more automatic retries.'))
    owner_id = event.data['subject_owner_id']
    owner = User.objects.filter(pk=owner_id).values_list('username', flat=True).first()
    fields.append({'name': 'Owner', 'value': safe_text(owner or 'Unknown'), 'inline': True})
    actor_id = event.data.get('actor_id')
    if actor_id:
        actor = User.objects.filter(pk=actor_id).values_list('username', flat=True).first()
        fields.append({'name': 'Acting user', 'value': safe_text(actor or 'Unknown'), 'inline': True})
    return {'title': title[:256], 'url': config.canonical_url + path, 'description': '\n'.join(description),
            'color': color, 'fields': fields, 'timestamp': event.created.isoformat()}


def enqueue_event(event, subject):
    key = normalize(event, subject)
    if not key:
        return
    config = DiscordConfiguration.objects.select_for_update().filter(pk=1, enabled=True).first()
    if not config or not config.webhook_ciphertext or not config.canonical_url:
        return
    owner_id = event.data['subject_owner_id']
    prefs = NotificationPreferences.objects.select_for_update(of=('self',)).filter(user_id=owner_id, enabled=True, user__is_active=True).first()
    mode = prefs.events.get(key, 'off') if prefs else 'off'
    if mode not in ('message', 'ping'):
        return
    NotificationDelivery.objects.get_or_create(event=event, recipient_id=owner_id, defaults={
        'event_key': key, 'subject_key': '%s:%s' % (event.subject_type, event.subject_id),
        'generation': config.generation, 'summary': snapshot(event, subject, key, config), 'ping_requested': mode == 'ping',
    })


def delivery_payload(delivery, prefs=None):
    user_id = prefs.discord_user_id if prefs and delivery.ping_requested and prefs.events.get(delivery.event_key) == 'ping' else ''
    if not SNOWFLAKE.fullmatch(user_id):
        user_id = ''
    return {'content': '<@%s>' % user_id if user_id else '', 'embeds': [copy.deepcopy(delivery.summary)],
            'allowed_mentions': {'parse': [], 'users': [user_id] if user_id else [], 'roles': [], 'replied_user': False}}


def eligible(delivery, config):
    if delivery.generation != config.generation or not config.webhook_ciphertext:
        return False, None
    user = User.objects.filter(pk=delivery.recipient_id, is_active=True).first()
    if delivery.event_key == 'admin.test':
        return bool(user and user.is_superuser), None
    prefs = NotificationPreferences.objects.filter(user=user, enabled=True).first() if user else None
    return bool(config.enabled and prefs and prefs.events.get(delivery.event_key) in ('message', 'ping')), prefs


@transaction.atomic
def claim_delivery():
    now = timezone.now()
    config, _ = DiscordConfiguration.objects.get_or_create(pk=1)
    token = uuid.uuid4()
    # One global webhook lease also serializes Discord rate-limit accounting.
    acquired = DiscordConfiguration.objects.filter(pk=1).filter(Q(lease_until=None) | Q(lease_until__lte=now)).update(
        lease_token=token, lease_until=now + timedelta(seconds=120), sender_seen=now)
    if not acquired:
        return None
    config.refresh_from_db()
    NotificationDelivery.objects.filter(status='sending', claimed_until__lte=now).update(status='pending', claim_token=None)
    # Recheck settings even for delayed rows, so disabling cannot replay them later.
    for row in NotificationDelivery.objects.filter(status='pending').order_by('id'):
        if not eligible(row, config)[0]:
            NotificationDelivery.objects.filter(pk=row.pk).update(status='skipped', last_error='Delivery disabled or configuration replaced.')
        elif row.created <= now - timedelta(hours=24) or row.attempts >= 8:
            NotificationDelivery.objects.filter(pk=row.pk).update(status='failed', last_error='Delivery retry limit reached.')
    if config.next_send_at <= now:
        earlier = NotificationDelivery.objects.filter(subject_key=OuterRef('subject_key'), id__lt=OuterRef('id'), status__in=ACTIVE)
        row = NotificationDelivery.objects.filter(status='pending', next_attempt_at__lte=now).annotate(blocked=Exists(earlier)).filter(blocked=False).order_by('id').first()
        if row:
            row.status, row.claim_token, row.claimed_until = 'sending', token, now + timedelta(seconds=120)
            row.attempts += 1
            row.save(update_fields=['status', 'claim_token', 'claimed_until', 'attempts'])
            return row, config
    DiscordConfiguration.objects.filter(pk=1, lease_token=token).update(lease_token=None, lease_until=None)
    return None


def retry_seconds(value, default=1):
    try:
        seconds = float(value)
        return max(1, seconds) if math.isfinite(seconds) and seconds <= 86400 else 86400
    except (ValueError, TypeError):
        return default


def send_once():
    claimed = claim_delivery()
    if not claimed:
        return False
    row, config = claimed
    config.refresh_from_db()
    allowed, prefs = eligible(row, config)
    status, error, message_id, delay = 'skipped', '', '', 0
    rate_delay = 0
    if allowed:
        try:
            url = decrypt_webhook(config)
            response = requests.post(url, params={'wait': 'true'}, json=delivery_payload(row, prefs), timeout=(5, 20), allow_redirects=False)
            code = response.status_code
            if response.headers.get('X-RateLimit-Remaining') == '0':
                rate_delay = retry_seconds(response.headers.get('X-RateLimit-Reset-After'))
            if code == 429:
                try:
                    body = response.json()
                    retry_after = body.get('retry_after') if isinstance(body, dict) else None
                except ValueError:
                    retry_after = None
                delay = max(retry_seconds(response.headers.get('Retry-After')), retry_seconds(retry_after))
                rate_delay = max(rate_delay, delay)
                status, error = 'pending', 'Discord rate limit (HTTP 429).'
            elif 500 <= code < 600:
                status, error = 'pending', 'Discord temporarily unavailable (HTTP %d).' % code
            elif code == 200:
                try:
                    body = response.json()
                    message_id = str(body.get('id', '')) if isinstance(body, dict) else ''
                except ValueError:
                    message_id = ''
                if SNOWFLAKE.fullmatch(message_id):
                    status = 'sent'
                else:
                    status, error = 'pending', 'Discord confirmation was missing; delivery may have succeeded.'
            else:
                status, error = 'failed', 'Discord rejected delivery (HTTP %d).' % code
        except requests.RequestException:
            status, error = 'pending', 'Discord network failure; delivery may have succeeded.'
        except ValidationError:
            status, error = 'failed', 'Discord credential unavailable. Restore the key or replace the webhook.'
    now = timezone.now()
    if status == 'pending':
        delay = max(delay, min(3600, 5 * 2 ** (row.attempts - 1)))
        if row.attempts >= 8 or now + timedelta(seconds=delay) >= row.created + timedelta(hours=24):
            status = 'failed'
    with transaction.atomic():
        # A stale sender must never overwrite a reclaimed delivery or new config.
        current = DiscordConfiguration.objects.select_for_update().get(pk=1)
        if current.lease_token == row.claim_token:
            NotificationDelivery.objects.filter(pk=row.pk, claim_token=row.claim_token, status='sending').update(
                status=status, last_error=error, message_id=message_id if status == 'sent' else '',
                next_attempt_at=now + timedelta(seconds=delay), claim_token=None, claimed_until=None)
            updates = {'lease_token': None, 'lease_until': None, 'sender_seen': now, 'next_send_at': now + timedelta(seconds=rate_delay)}
            if status == 'sent':
                updates['last_success'] = now
            DiscordConfiguration.objects.filter(pk=1).update(**updates)
    return True
