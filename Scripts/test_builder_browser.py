"""Serve the real builder template with an isolated, in-memory schedule for browser tests."""
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
from django.template.loader import render_to_string
from django.core.exceptions import ValidationError
from OpenBench.schedule_builder import DEFAULT_SPEC, generate_schedule
spec, files, _ = generate_schedule(DEFAULT_SPEC)
state = {'spec': spec, 'source': files['examples/mattbench.rs'], 'post_url': '/builder/', 'version': 0, 'scope': 'personal', 'name': 'Browser test', 'engine': ''}

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, data, content_type, status=200):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path.startswith('/static/'):
            file = ROOT / 'OpenBench/static' / path.removeprefix('/static/')
            if file.is_file() and file.resolve().is_relative_to((ROOT / 'OpenBench/static').resolve()):
                return self.send(file.read_bytes(), 'text/javascript' if file.suffix == '.js' else 'text/css')
            return self.send(b'', 'text/plain', 404)
        self.send(render_to_string('OpenBench/schedule_builder.html', {'builder_data': state, 'csrf_token': 'test-token', 'static_version': 1}).encode(), 'text/html')

    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        try:
            spec, files, _ = generate_schedule(data['spec'])
            response = {'source': files['examples/mattbench.rs']}
            if data['action'] == 'save':
                state.update(spec=spec, source=response['source'], name=data['name'], version=state['version'] + 1)
                response.update(spec=spec, version=state['version'], url='/builder/', source_url='/source/', train_url='/train/')
            self.send(json.dumps(response).encode(), 'application/json')
        except ValidationError as error:
            self.send(json.dumps({'error': '; '.join(error.messages)}).encode(), 'application/json', 400)

server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
print('http://127.0.0.1:%d/builder/' % server.server_port, flush=True)
server.serve_forever()
