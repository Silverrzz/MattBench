import json
import uuid
from pathlib import Path

from django.core.management.base import BaseCommand
from django.db import transaction

from OpenBench.models import TrainingSchedule
from OpenBench.training import validate_schedule


class Command(BaseCommand):
    help = 'Import the bundled Bullet examples as global training schedules.'

    def add_arguments(self, parser):
        parser.add_argument('--remove-demos', action='store_true')

    @transaction.atomic
    def handle(self, *args, **options):
        path = Path(__file__).resolve().parents[2] / 'data' / 'bullet_schedules.json'
        presets = json.loads(path.read_text(encoding='utf-8'))
        created = 0
        for preset in presets:
            config = validate_schedule(preset['files'], preset['settings'])
            identifier = uuid.uuid5(uuid.NAMESPACE_URL, '%s/blob/%s' % (config['bullet_repo'], config['example_source']))
            _, added = TrainingSchedule.objects.get_or_create(pk=identifier, defaults={
                'name': preset['name'], 'engine': None, 'owner': None,
                'files': preset['files'], 'settings': config,
            })
            created += added
        removed = 0
        if options['remove_demos']:
            for schedule in TrainingSchedule.objects.filter(files__has_key='mattbench-demo.json'):
                try:
                    marker = json.loads(schedule.files['mattbench-demo.json'])
                except (TypeError, ValueError):
                    continue
                if marker == {'demo': 'mattbench-ui-v1'}:
                    schedule.delete()
                    removed += 1
        self.stdout.write(self.style.SUCCESS('Imported %d global Bullet schedules; kept %d existing; removed %d demo schedules.' % (created, len(presets) - created, removed)))
