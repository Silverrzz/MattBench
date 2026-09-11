from OpenBench.models import LifecycleEvent


def record_event(kind, subject, owner_id, data=None, key=None):
    subject_type = subject._meta.model_name
    subject_id = str(subject.pk)
    return LifecycleEvent.objects.get_or_create(
        key=key or '%s:%s:%s' % (kind, subject_type, subject_id),
        defaults={'kind': kind, 'subject_type': subject_type, 'subject_id': subject_id, 'owner_id': owner_id, 'data': data or {}},
    )[0]
