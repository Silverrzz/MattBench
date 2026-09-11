import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import IntegrityError, close_old_connections, transaction
from django.utils import timezone

from OpenBench.models import TrainingServiceLease
from OpenBench.training_tasks import TrainingTasks


class Command(BaseCommand):
    help = 'Run the supervised training coordinator. Deploy one service independently of the web server.'

    def add_arguments(self, parser):
        parser.add_argument('--lane', choices=('validation', 'uploads', 'maintenance'))
        parser.add_argument('--status', action='store_true')

    def handle(self, *args, **options):
        if options['status']:
            now = timezone.now()
            active = set(TrainingServiceLease.objects.filter(name__startswith='coordinator-', expires__gt=now).values_list('name', flat=True))
            for lane in ('validation', 'uploads', 'maintenance'):
                self.stdout.write('%s: %s' % (lane, 'running' if 'coordinator-' + lane in active else 'offline'))
            if len(active) != 3:
                raise CommandError('The training coordinator is not fully available.')
            return
        stop = threading.Event()
        for signum in (signal.SIGINT, signal.SIGTERM):
            signal.signal(signum, lambda *args: stop.set())
        if options['lane']:
            self.lane(options['lane'], stop)
            return
        processes = {}
        self.stdout.write('Training coordinator supervisor started.')
        environment = {**os.environ, 'OPENBENCH_DISABLE_WATCHERS': '1', 'PYTHONUNBUFFERED': '1'}
        try:
            while not stop.is_set():
                for lane in ('validation', 'uploads', 'maintenance'):
                    process = processes.get(lane)
                    if process is None or process.poll() is not None:
                        if process is not None:
                            self.stderr.write('Restarting training %s process (exit %s).' % (lane, process.returncode))
                        processes[lane] = subprocess.Popen([sys.executable, str(Path(settings.BASE_DIR) / 'manage.py'), 'training_tasks', '--lane', lane], env=environment)
                stop.wait(5)
        finally:
            for process in processes.values():
                if process.poll() is None:
                    process.terminate()
            for process in processes.values():
                try:
                    process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()

    def lane(self, name, stop):
        key = 'coordinator-' + name
        token = uuid.uuid4()
        now = timezone.now()
        try:
            TrainingServiceLease.objects.get_or_create(name=key, defaults={'token': token, 'expires': now})
            claimed = TrainingServiceLease.objects.filter(pk=key, expires__lte=now).update(token=token, expires=now + timedelta(seconds=60))
            if not claimed:
                raise CommandError('Another coordinator owns the %s lease.' % name)
        except IntegrityError:
            raise CommandError('Another coordinator acquired the %s lease.' % name) from None
        tasks = TrainingTasks(stop)
        deadline = settings.TRAINING_UPLOAD_TIMEOUT if name == 'uploads' else settings.TRAINING_VALIDATION_TIMEOUT
        def renew():
            while not stop.wait(10):
                if tasks.active_started is not None and time.monotonic() - tasks.active_started > deadline:
                    self.stderr.write('Training %s operation exceeded its deadline; restarting.' % name)
                    os._exit(3)
                close_old_connections()
                try:
                    now = timezone.now()
                    if not TrainingServiceLease.objects.filter(pk=key, token=token, expires__gt=now).update(expires=now + timedelta(seconds=60)):
                        os._exit(2)
                except Exception:
                    os._exit(2)
        thread = threading.Thread(target=renew, daemon=True)
        thread.start()
        try:
            tasks.run_lane(name)
        finally:
            stop.set()
            thread.join(timeout=15)
            TrainingServiceLease.objects.filter(pk=key, token=token).update(expires=timezone.now())
