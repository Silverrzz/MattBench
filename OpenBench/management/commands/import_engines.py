from OpenBench.management.commands.import_config import Command as ImportCommand


class Command(ImportCommand):

    help = 'Bulk import engine JSON files from a directory, including their shared presets.'
    kind = 'engines'
