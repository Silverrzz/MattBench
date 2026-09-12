import logging
import uuid

from django.contrib.auth.models import User
from django.db import transaction

from OpenBench.models import LifecycleEvent

logger = logging.getLogger(__name__)


@transaction.atomic
def record_event(kind, subject, owner_id, data=None, key=None, actor_id=None):
    subject_type = subject._meta.model_name
    subject_id = str(subject.pk)
    subject_owner_id = getattr(subject, 'owner_id', None) or getattr(subject, 'user_id', None) or owner_id
    if subject_type == 'test':
        subject_owner_id = User.objects.filter(username=subject.author).values_list('pk', flat=True).first()
        if not subject_owner_id:
            logger.warning('Lifecycle event skipped: unresolved test author, test_id=%s', subject.pk)
            return None
    if actor_id is None and owner_id != subject_owner_id:
        actor_id = owner_id
    data = {**(data or {}), 'subject_owner_id': subject_owner_id, 'actor_id': actor_id}
    event, created = LifecycleEvent.objects.get_or_create(
        key=key or '%s:%s:%s' % (kind, subject_type, subject_id),
        defaults={'kind': kind, 'subject_type': subject_type, 'subject_id': subject_id, 'owner_id': owner_id or subject_owner_id, 'data': data},
    )
    if created:
        from OpenBench.discord_notifications import enqueue_event
        enqueue_event(event, subject)
    return event


def test_event(action, test, actor_id=None):
    owner_id = User.objects.filter(username=test.author).values_list('pk', flat=True).first()
    # Controls are called only after a locked state transition; execution events
    # use a stable generation key so report retries and assignments consolidate.
    suffix = str(uuid.uuid4()) if action in ('stopped', 'restarted', 'deleted', 'restored', 'approved') else str(test.execution_number)
    kind = test.workload_type_str() + '.' + action
    return record_event(kind, test, owner_id, key='%s:%s:%s' % (kind, test.pk, suffix), actor_id=actor_id)


def worker_mode_event(subject, mode, actor_id):
    if subject.mode == mode:
        return
    action = 'pause_requested' if mode == 'paused' else 'resumed' if subject.mode == 'paused' else None
    if action:
        record_event('worker.' + action, subject, actor_id, key='worker.mode:%s:%s' % (subject.pk, uuid.uuid4()), actor_id=actor_id)
