import signal
import sys

bind = '127.0.0.1:8000'
workers = 2
worker_class = 'uvicorn_worker.UvicornWorker'
chdir = '/opt/mattbench-prod'
preload_app = False
reload = False
max_requests = 0
timeout = 30
graceful_timeout = 60
keepalive = 2
forwarded_allow_ips = '127.0.0.1'
accesslog = '-'
errorlog = '-'
loglevel = 'info'
capture_output = True


def exit_cleanly(signum, frame):
    signal.signal(signum, signal.SIG_IGN)
    sys.exit(0)


def post_worker_init(worker):
    signal.signal(signal.SIGTERM, exit_cleanly)
    signal.signal(signal.SIGINT, exit_cleanly)
    from django.apps import apps
    from django.conf import settings
    app = apps.get_app_config('OpenBench')
    watcher = getattr(app, 'pgn_watcher', None)
    worker.log.info('OpenBench ready: HTML_MINIFY=%s database=%s PGN_watcher=%s',
                    settings.HTML_MINIFY, settings.DATABASES['default']['ENGINE'],
                    bool(watcher and watcher.is_alive()))
