import inspect
import json
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from django.core.exceptions import ValidationError
from django.test import RequestFactory, SimpleTestCase

from Client.training_data import upload_prepared_files
from OpenBench import training_datasets


class PreparedUploadTests(SimpleTestCase):
    descriptor = {'path': 'data/run-41-0.0/00000.vf', 'size': 10, 'sha256': 'a' * 64}

    def instruction(self, upload):
        request = RequestFactory().post('/api/training/41/dataset/prepare/upload/',
            data=json.dumps(self.descriptor), content_type='application/json')
        response = Mock(json=Mock(return_value={'objects': [{
            'oid': self.descriptor['sha256'], 'size': 10, 'actions': {'upload': upload},
        }]}))
        context = (SimpleNamespace(owner_id=1), '0.0', {'repo': 'test/dataset'},
                   {'prefix': 'data/run-41-0.0/', 'branch': 'main'})
        with patch.object(training_datasets, 'publication_context', return_value=context), \
             patch.object(training_datasets, 'hf_token', return_value='account-secret'), \
             patch.object(training_datasets.requests, 'post', return_value=response):
            result = inspect.unwrap(training_datasets.dataset_upload)(request, 41)
        return json.loads(result.content)

    def test_padded_parts_upload_and_complete_with_existing_worker(self):
        for keys in [('00001', '00002', '00003'), ('1', '2', '3')]:
            with self.subTest(keys=keys), tempfile.TemporaryDirectory() as temp:
                action = {'href': 'https://unused.invalid/complete', 'header': {
                    'chunk_size': '4', keys[2]: 'https://unused.invalid/3',
                    keys[0]: 'https://unused.invalid/1', keys[1]: 'https://unused.invalid/2',
                }}
                instruction = self.instruction(action)
                self.assertNotIn('account-secret', json.dumps(instruction))
                path = Path(temp) / 'data.vf'
                path.write_bytes(b'valid-game')
                reporter = Mock(metrics={}, stop=threading.Event())
                reporter.update.side_effect = lambda **values: reporter.metrics.update(values)
                connection = Mock()
                connection.request.side_effect = lambda *args, **kwargs: Mock(json=Mock(return_value=
                    {'stored': True} if kwargs['json'].get('verify') else instruction))
                sent = []
                def put(url, data, **kwargs):
                    contents = b''
                    while chunk := data.read():
                        contents += chunk
                    sent.append((url, contents))
                    response = MagicMock()
                    response.__enter__.return_value.headers = {'etag': 'etag-%d' % len(sent)}
                    return response
                with patch('Client.training_data.requests.put', side_effect=put), \
                     patch('Client.training_data.requests.post', return_value=MagicMock()) as complete:
                    upload_prepared_files(connection, {'id': 41}, [path], [self.descriptor], reporter)
                self.assertEqual(sent, [('https://unused.invalid/1', b'vali'),
                                        ('https://unused.invalid/2', b'd-ga'),
                                        ('https://unused.invalid/3', b'me')])
                complete.assert_called_once_with(action['href'], json={'oid': 'a' * 64,
                    'parts': [{'partNumber': number, 'etag': 'etag-%d' % number} for number in range(1, 4)]},
                    headers={'Content-Type': 'application/vnd.git-lfs+json'}, timeout=(15, 60))
                self.assertEqual(reporter.metrics['progress'], 100)

    def test_basic_upload_headers_are_preserved(self):
        action = {'href': 'https://unused.invalid/upload', 'header': {'Content-Type': 'application/octet-stream'}}
        self.assertEqual(self.instruction(action)['upload'], action)

    def test_invalid_multipart_instructions_are_rejected(self):
        for header in [
            {'chunk_size': '4', '00001': 'url', '00003': 'url'},
            {'chunk_size': '4', '00001': 'url', '1': 'duplicate', '2': 'url', '3': 'url'},
            {'chunk_size': '0'},
            {'chunk_size': '4', '00001': 'url', '00002': None, '00003': 'url'},
        ]:
            with self.subTest(header=header), self.assertRaises(ValidationError):
                self.instruction({'href': 'https://unused.invalid/complete', 'header': header})
