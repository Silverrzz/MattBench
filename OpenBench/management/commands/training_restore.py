import json
import shutil
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from OpenBench.models import TrainingRun
from OpenBench.training_storage import offline_operation, sync_file, valid_file


class Command(BaseCommand):
    help = 'Restore a verified training backup into an empty, migrated deployment with web/coordinator services stopped.'

    def add_arguments(self, parser):
        parser.add_argument('directory', type=Path)

    @offline_operation
    def handle(self, *args, **options):
        if get_user_model().objects.exists() or TrainingRun.objects.exists():
            raise CommandError('Restore requires an empty deployment; existing data is never overwritten.')
        root = options['directory'].resolve()
        manifest = json.loads((root / 'manifest.json').read_text(encoding='utf-8'))
        if manifest.get('version') != 1:
            raise CommandError('Unsupported backup format.')
        destinations = []
        seen = set()
        for item in manifest['files']:
            relative = item['path']
            source = (root / relative).resolve()
            if relative in seen or not source.is_relative_to(root) or not valid_file(source, item['size'], item['sha256']):
                raise CommandError('Backup verification failed: ' + relative)
            seen.add(relative)
            if relative == 'database.json':
                continue
            prefix, suffix = relative.split('/', 1)
            if prefix not in ('training', 'media'):
                raise CommandError('Unexpected backup path.')
            target_root = Path(settings.TRAINING_ROOT if prefix == 'training' else settings.MEDIA_ROOT).resolve()
            target = (target_root / suffix).resolve()
            if not target.is_relative_to(target_root):
                raise CommandError('Restore destination is invalid: ' + relative)
            if target.exists():
                if not valid_file(target, item['size'], item['sha256']):
                    raise CommandError('Restore destination contains a different file: ' + relative)
                continue
            destinations.append((source, target))
        if 'database.json' not in seen:
            raise CommandError('The backup has no database snapshot.')
        for source, target in destinations:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            sync_file(target)
        with transaction.atomic():
            call_command('loaddata', str(root / 'database.json'))
        call_command('training_storage', repair=True)
        self.stdout.write('Restore complete. Configure the original credential encryption key before starting services.')
