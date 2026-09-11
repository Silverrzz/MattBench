import shutil
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from OpenBench.models import TrainingArtifact
from OpenBench.training_storage import artifact_path, replicate, storage_lock, sync_file, valid_file


class Command(BaseCommand):
    help = 'Verify artifact checksums, restore damaged primary files from replicas, and optionally remove old unreferenced files.'

    def add_arguments(self, parser):
        parser.add_argument('--repair', action='store_true')
        parser.add_argument('--remove-orphans', action='store_true')

    def handle(self, *args, **options):
        failures = []
        with storage_lock():
            paths = set()
            for row in TrainingArtifact.objects.iterator():
                paths.add(row.path)
                primary = artifact_path(row.path)
                if not valid_file(primary, row.size, row.sha256):
                    replica = artifact_path(row.path, replica=True) if settings.TRAINING_REPLICA_ROOT else None
                    if options['repair'] and replica and valid_file(replica, row.size, row.sha256):
                        primary.parent.mkdir(parents=True, exist_ok=True)
                        temporary = primary.with_suffix('.repair.part')
                        shutil.copyfile(replica, temporary)
                        sync_file(temporary)
                        temporary.replace(primary)
                        sync_file(primary)
                    else:
                        failures.append(str(row.pk))
                        continue
                if settings.TRAINING_REPLICA_ROOT:
                    if options['repair']:
                        replicate(row.path, row.size, row.sha256)
                    elif not valid_file(artifact_path(row.path, replica=True), row.size, row.sha256):
                        failures.append(str(row.pk) + ' replica')
            cutoff = (timezone.now() - timedelta(days=1)).timestamp()
            for configured in (settings.TRAINING_ROOT, settings.TRAINING_REPLICA_ROOT):
                if not configured:
                    continue
                root = Path(configured).resolve()
                for path in root.glob('*/artifacts/*'):
                    if not path.is_symlink() and path.is_file() and path.resolve().is_relative_to(root) and path.relative_to(root).as_posix() not in paths and path.stat().st_mtime < cutoff:
                        self.stdout.write('Unreferenced: ' + str(path))
                        if options['remove_orphans']:
                            path.unlink()
        if failures:
            raise CommandError('Missing or corrupt artifacts: ' + ', '.join(failures))
        self.stdout.write('Artifact storage verified.')
