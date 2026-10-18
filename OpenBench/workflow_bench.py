from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from OpenBench.models import Engine, Test


@transaction.atomic
def record(data, machine):
    try:
        identifier, engine_id, nodes = int(data['test_id']), int(data['engine_id']), int(data['bench'])
        side = data['branch']
        if side not in ('dev', 'base') or not 0 < nodes <= 2147483647:
            raise ValueError
        test = Test.objects.select_for_update().get(pk=identifier)
    except (KeyError, ValueError, TypeError, Test.DoesNotExist):
        raise ValidationError('Invalid workflow bench report.') from None
    if not test.execution.get('workflow') or test.finished or test.deleted or machine.workload != test.pk or not test.test.filter(machine=machine).exists():
        raise ValidationError('This workflow workload assignment is closed.')
    engine = Engine.objects.select_for_update().get(pk=getattr(test, side + '_id'))
    automatic = side in test.execution.get('automatic_bench', []) or side in test.execution.get('bench_discoveries', {}) or not engine.bench
    if not automatic or engine.pk != engine_id or engine.sha != data.get('sha') or getattr(test, side + '_network') != data.get('network'):
        raise ValidationError('Bench report differs from the assigned engine and network.')
    if not engine.bench:
        engine.bench = nodes
        engine.save(update_fields=['bench'])
        discoveries = test.execution.get('bench_discoveries', {})
        test.execution = {**test.execution, 'bench_discoveries': {**discoveries, side: {'bench': nodes, 'sha': engine.sha, 'network': data['network'], 'machine': machine.pk, 'recorded': timezone.now().isoformat()}}}
        test.save(update_fields=['execution', 'updated'])
    return engine.bench
