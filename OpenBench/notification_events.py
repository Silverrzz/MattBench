"""Public notification vocabulary, independent of the lifecycle API vocabulary."""
GROUPS = {
    'test': ('Tests — SPRT and fixed games', 'created approved started passed failed execution_error stopped restarted deleted restored'),
    'tune': ('SPSA tuning', 'created approved started completed execution_error stopped restarted deleted restored'),
    'datagen': ('Datagen', 'created approved started completed execution_error stopped restarted deleted restored'),
    'training': ('Training', 'created queued started completed failed cancel_requested cancelled interrupted recovered continued deleted restored'),
    'upload': ('Dataset uploads', 'completed failed'),
    'worker': ('Workers', 'registered disconnected pause_requested resumed'),
}
LABELS = {'execution_error': 'Execution error', 'queued': 'Initially queued', 'cancel_requested': 'Cancellation requested',
          'continued': 'Continued from checkpoint', 'pause_requested': 'Pause requested', 'disconnected': 'Explicitly disconnected'}
EVENTS = {family + '.' + event: LABELS.get(event, event.capitalize())
          for family, (_, events) in GROUPS.items() for event in events.split()}
MODES = (('off', 'Off'), ('message', 'Message'), ('ping', 'Message + ping'))


def default_events():
    selected = {'passed', 'completed', 'failed', 'execution_error', 'interrupted'}
    return {key: 'message' if key.split('.')[-1] in selected else 'off' for key in EVENTS}


def normalize(event, subject):
    kind = event.kind
    if kind == 'datagen.uploaded':
        return 'upload.completed'
    if kind == 'training.task.failed':
        return 'upload.failed' if event.subject_type == 'datasetupload' else 'training.failed'
    if kind == 'worker.connected':
        # Linking a training capability to an already registered machine is silent.
        return 'worker.registered' if event.data.get('registered') and not subject.machine_id else None
    if kind == 'training.failed' and subject.metrics.get('recovery_pending'):
        return None  # The explicit recovery episode emits an interruption event.
    kind = {'training.cancel.requested': 'training.cancel_requested', 'training.resumed': 'training.continued'}.get(kind, kind)
    return kind if kind in EVENTS else None
