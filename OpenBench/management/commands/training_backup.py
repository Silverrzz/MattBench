import hashlib
import json
import shutil
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.utils import timezone

from OpenBench.models import TrainingArtifact
from OpenBench.training_storage import artifact_path, offline_operation, storage_lock, sync_file, valid_file


class Command(BaseCommand):
    help = 'Create a database and training-artifact backup in a new directory. Keep the credential encryption key separately.'

    def add_arguments(self, parser):
        parser.add_argument('directory', type=Path)

    @offline_operation
    @transaction.atomic
    def handle(self, *args, **options):
        if connection.vendor == 'postgresql':
            with connection.cursor() as cursor:
                cursor.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ')
        destination = options['directory'].resolve()
        if any(destination.is_relative_to(Path(root).resolve()) for root in (settings.TRAINING_ROOT, settings.MEDIA_ROOT)):
            raise CommandError('Backups must be outside live storage roots.')
        destination.mkdir(parents=True, exist_ok=False)
        files = []
        def capture(source, relative):
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            sync_file(target)
            with target.open('rb') as stream:
                digest = hashlib.file_digest(stream, 'sha256').hexdigest()
            files.append({'path': relative, 'size': target.stat().st_size, 'sha256': digest})
        with storage_lock():
            fixture = destination / 'database.json'
            with fixture.open('w', encoding='utf-8') as output:
                call_command('dumpdata', use_base_manager=True, natural_foreign=True, exclude=['contenttypes', 'auth.permission', 'sessions'], stdout=output)
            sync_file(fixture)
            with fixture.open('rb') as source:
                files.append({'path': 'database.json', 'size': fixture.stat().st_size, 'sha256': hashlib.file_digest(source, 'sha256').hexdigest()})
            for row in TrainingArtifact.objects.iterator():
                source = artifact_path(row.path)
                if not valid_file(source, row.size, row.sha256):
                    raise CommandError('Artifact %s is missing or corrupt. Repair storage first.' % row.pk)
                capture(source, 'training/' + row.path)
            media = Path(settings.MEDIA_ROOT).resolve()
            for source in media.rglob('*'):
                if source.is_file() and not source.is_symlink() and source.resolve().is_relative_to(media):
                    capture(source, 'media/' + source.relative_to(media).as_posix())
            manifest = destination / 'manifest.json'
            manifest.write_text(json.dumps({'version': 1, 'created': timezone.now().isoformat(), 'files': files}, indent=2), encoding='utf-8')
            sync_file(manifest)
        self.stdout.write('Backup complete: %s. Store the credential key separately; restore using training_restore on an empty deployment.' % destination)
