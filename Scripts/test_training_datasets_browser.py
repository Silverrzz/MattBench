"""Serve the real training form with static fixtures for browser regression tests."""
import copy
import json
import os
from pathlib import Path
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'OpenSite.settings')
os.environ['OPENBENCH_DISABLE_WATCHERS'] = '1'
import django
django.setup()
from django import forms
from django.template.loader import render_to_string
from OpenBench.training_views import TrainingForm


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        path = urlsplit(self.path).path
        if path.startswith('/static/'):
            asset = ROOT / 'OpenBench/static' / path.removeprefix('/static/')
            if not asset.is_file() or not asset.resolve().is_relative_to((ROOT / 'OpenBench/static').resolve()):
                self.send_error(404)
                return
            data = asset.read_bytes()
            content_type = 'text/javascript' if asset.suffix == '.js' else 'text/css'
        else:
            form = forms.Form(initial={'engine': 'engine', 'schedule': 'schedule', 'name': 'Test',
                'stage_datasets': [{'stage': 0, 'dataset': 'a'}, {'stage': 1, 'dataset': 'b'}]})
            form.fields = copy.deepcopy(TrainingForm.base_fields)
            for name, field in form.fields.items():
                field.widget.attrs['id'] = 'train-' + name
            form.fields['engine'].choices = [('', 'Choose engine'), ('engine', 'Test engine')]
            form.fields['schedule'].choices = [('', 'Choose schedule'), ('schedule', 'Two stages'), ('other', 'Custom schedule')]
            form.fields['checkpoint'].choices = [('', 'Start fresh'), ('checkpoint', 'Resume test')]
            data = render_to_string('OpenBench/training_new.html', {'form': form, 'csrf_token': 'test-token',
                'static_version': 1, 'hf_connected': True,
                'schedule_options': [{'id': 'schedule', 'engine': '', 'name': 'Two stages', 'scope': 'Personal', 'workloads': True,
                    'stages': [{'start': 1, 'end': 10}, {'start': 11, 'end': 20}]},
                    {'id': 'other', 'engine': '', 'name': 'Custom schedule', 'scope': 'Personal', 'workloads': False, 'stages': []}],
                'dataset_options': [{'id': id, 'name': name, 'repo': 'test/' + id, 'revision': 'main', 'owner': 'Alice', 'is_owner': True}
                    for id, name in [('a', 'Alpha'), ('b', 'Beta'), ('c', 'Gamma')]],
                'checkpoint_options': [{'id': 'checkpoint', 'engine': 'engine', 'schedule': 'schedule', 'superbatch': 10,
                    'run': 1, 'name': 'Resume', 'datasets': [['a', 'c'], ['b', 'c']]}]}).encode()
            content_type = 'text/html'
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.end_headers()
        self.wfile.write(data)


server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
print('http://127.0.0.1:%d/' % server.server_port, flush=True)
server.serve_forever()
