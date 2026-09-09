from OpenBench.management.commands.import_config import Command as ImportCommand


class Command(ImportCommand):

    help = 'Sync opening-book JSON files from a directory. Use --replace to update existing books.'
    kind = 'books'
