from unittest.mock import Mock, patch

from django.contrib.auth.models import User
from django.test import RequestFactory, TestCase

import OpenBench.views
from OpenBench.config import _request_config
from OpenBench.models import Profile, Test
from OpenBench.workloads.verify_workload import strip_message


class WorkloadInformationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('workload-author')
        Profile.objects.create(user=self.user, enabled=True)
        self.message = 'Improve search (bench 5983062)\n\n' + 'Explain the search changes.\n' * 60
        self.config = {
            'engines': {'Example': {'private': False, 'source': 'https://github.com/example/engine', 'variants': ['standard']}},
            'books': {'book.epd': {'variants': ['standard']}},
            'variants': {'standard': {'syzygy': True}},
            'use_cross_approval': True,
        }
        token = _request_config.set(self.config)
        self.addCleanup(_request_config.reset, token)
        self.data = {
            'dev_engine': 'Example', 'base_engine': 'Example',
            'dev_repo': 'https://github.com/example/engine', 'base_repo': 'https://github.com/example/engine',
            'dev_branch': 'feature', 'base_branch': 'master',
            'dev_bench': 'Autofill', 'base_bench': 'Autofill',
            'dev_network': '', 'base_network': '',
            'dev_options': 'Threads=1 Hash=16', 'base_options': 'Threads=1 Hash=16',
            'dev_time_control': '8.0+0.08', 'base_time_control': '8.0+0.08',
            'test_mode': 'SPRT', 'test_bounds': '[0.00, 3.00]', 'test_confidence': '[0.10, 0.05]',
            'book_name': 'book.epd', 'upload_pgns': 'FALSE',
            'priority': '0', 'throughput': '1000', 'workload_size': '32',
            'syzygy_wdl': 'DISABLED', 'syzygy_adj': 'OPTIONAL',
            'win_adj': 'movecount=8 score=800', 'draw_adj': 'movenumber=32 movecount=10 score=0',
            'scale_method': 'BASE', 'scale_nps': '1000000', 'info': '',
        }

    def github_response(self, url, **kwargs):
        if '/compare/' in url:
            return Mock(json=Mock(return_value={'ahead_by': 0}))
        sha = ('a' if url.endswith('/feature') else 'b') * 40
        return Mock(json=Mock(return_value={'commit': {'sha': sha, 'commit': {'message': self.message}}}))

    def create_test(self):
        request = RequestFactory().post('/test/new/', self.data)
        request.user, request.session = self.user, {}
        with patch('OpenBench.workloads.verify_workload.requests.get', side_effect=self.github_response):
            response = OpenBench.views.new_workload(request, 'test')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], '/index/', request.session)
        workload = Test.objects.get()
        # SQLite does not enforce varchar lengths; also check model validation.
        workload.full_clean()
        return workload

    def test_long_commit_message_is_preserved(self):
        expected = strip_message(self.message)
        self.assertGreater(len(expected), 1024)
        workload = self.create_test()
        self.assertEqual(workload.info, expected)
        self.assertEqual(workload.dev.bench, 5983062)
        self.assertEqual(Profile.objects.get(user=self.user).tests, 1)

    def test_long_custom_information_is_preserved(self):
        self.data['info'] = 'Search notes: café ♟\n' * 100
        self.assertGreater(len(self.data['info']), 1024)
        self.assertEqual(self.create_test().info, self.data['info'])
