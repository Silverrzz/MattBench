from pathlib import Path
import json

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import IntegrityError, transaction
from OpenBench.configuration_import import import_directory


class Command(BaseCommand):

    help = 'Import Engines and Books JSON directories. Site settings remain in Config/config.json.'
    kind = None

    def add_arguments(self, parser):
        parser.add_argument('directory')
        parser.add_argument('--apply', action='store_true', help='Write changes; otherwise only validate and report.')
        parser.add_argument('--replace', action='store_true', help='Replace matching entries and shared presets. Personal presets are retained.')
        if self.kind == 'books':
            parser.add_argument('--disable-missing', action='store_true', help='Disable books whose JSON files are absent from this directory.')

    @transaction.atomic
    def handle(self, *args, **options):
        try:
            selection = {}
            if self.kind is None:
                root = Path(options['directory'])
                source = root / 'Config' / 'config.json'
                if source.exists():
                    data = json.loads(source.read_text(encoding='utf-8-sig'))
                    for kind in ('engines', 'books'):
                        if kind in data:
                            selection[kind] = list(data[kind])
            for kind in ([self.kind] if self.kind else ['engines', 'books']):
                directory = Path(options['directory'])
                if self.kind is None:
                    directory /= kind.title()
                counts = import_directory(directory, kind, options['apply'], options['replace'], options.get('disable_missing', False), selection.get(kind))
                self.stdout.write('%s: %d new, %d updated, %d skipped, %d disabled' %
                                  (kind, counts['new'], counts['updated'], counts['skipped'], counts['disabled']))
            self.stdout.write('Import complete.' if options['apply'] else 'Validation complete; use --apply to write changes.')
        except (ValidationError, ValueError, OSError, TypeError, IntegrityError) as error:
            raise CommandError(str(error)) from error
