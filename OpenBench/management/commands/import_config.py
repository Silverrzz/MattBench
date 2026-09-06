from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from OpenBench.configuration_import import read_legacy_config, import_legacy_config


class Command(BaseCommand):
    help = 'Validate legacy configuration; use --apply to import it into the database. Credentials are excluded.'

    def add_arguments(self, parser):
        parser.add_argument('directory')
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--replace', action='store_true')
        parser.add_argument('--actor', help='Superuser to record as the importing administrator')

    def handle(self, *args, **options):
        from django.contrib.auth import get_user_model
        from OpenBench.models import EngineConfig, OpeningBook, Credential
        try:
            actor = None
            if options['actor']:
                actor = get_user_model().objects.get(username=options['actor'], is_superuser=True, is_active=True)
            bundle = read_legacy_config(options['directory'])
            for kind, model in (('engines', EngineConfig), ('books', OpeningBook)):
                existing = set(model.objects.filter(name__in=bundle[kind]).values_list('name', flat=True))
                self.stdout.write('%s: %d new, %d existing' % (kind, len(bundle[kind]) - len(existing), len(existing)))
                for name in sorted(existing):
                    self.stdout.write('  Existing: %s' % name)
            self.stdout.write('Variants: %s' % ', '.join(bundle['variants']))
            self.stdout.write('Site settings and matching variants will be imported; --replace is required to overwrite customized settings.')
            for name, data in bundle['engines'].items():
                if data['settings']['private'] and not Credential.objects.filter(engine__name=name).exists():
                    self.stdout.write('Credential required separately: %s' % name)
            if options['apply']:
                revision = import_legacy_config(bundle, options['replace'], actor)
                self.stdout.write(self.style.SUCCESS('Published configuration generation %d' % revision.generation))
            else:
                self.stdout.write('Validation complete; no database changes. Use --apply to import.')
        except (ValidationError, ValueError, OSError, KeyError, TypeError, get_user_model().DoesNotExist) as error:
            raise CommandError(str(error)) from error
