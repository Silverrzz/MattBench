import signal
import threading

from django.core.management.base import BaseCommand
from django.db import close_old_connections

from OpenBench.discord_notifications import send_once


class Command(BaseCommand):
    help = 'Deliver queued Discord notifications independently of workload services.'

    def add_arguments(self, parser):
        parser.add_argument('--once', action='store_true', help='Process at most one eligible delivery and exit.')

    def handle(self, *args, **options):
        stop = threading.Event()
        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGTERM, lambda *_: stop.set())
            signal.signal(signal.SIGINT, lambda *_: stop.set())
        while not stop.is_set():
            close_old_connections()
            delivered = send_once()
            if options['once']:
                return
            if not delivered:
                stop.wait(2)
