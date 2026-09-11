import os
from pathlib import Path

from cryptography.fernet import Fernet
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = 'Create a credential encryption key. Keep it separate from the database and set MATTBENCH_CREDENTIAL_KEY_FILE to its path.'

    def add_arguments(self, parser):
        parser.add_argument('path', type=Path)

    def handle(self, *args, **options):
        path = options['path'].expanduser().resolve()
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, 'wb') as output:
                output.write(Fernet.generate_key() + b'\n')
        except FileExistsError:
            raise CommandError('The key file already exists. It was not changed.') from None
        except OSError:
            raise CommandError('Could not create the key file. Check the parent directory and permissions.') from None
        self.stdout.write('Created credential key. Set MATTBENCH_CREDENTIAL_KEY_FILE=%s and restart MattBench.' % path)
