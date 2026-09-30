from unittest import mock

from django.test import TestCase

from OpenBench.models import SPSARun, Test
from OpenBench.tests.fixtures import PASSWORD, create_engine_config, create_user, ensure_book

REPO = 'https://github.com/SnowballSH/Avalanche'

def github_commit(url, **kwargs):
    branch = { 'sha' : 'c' * 40, 'commit' : { 'message' : 'Change things\n\nBench: 1234567' } }
    return mock.Mock(**{ 'json.return_value' : { 'commit' : branch } })

def shared_fields(**overrides):
    return {
        'dev_engine' : 'Avalanche', 'dev_repo' : REPO, 'dev_branch' : 'dev', 'dev_bench' : '',
        'dev_network' : '', 'dev_options' : 'Threads=1 Hash=16', 'dev_time_control' : '8.0+0.08',
        'base_engine' : 'Avalanche', 'base_repo' : REPO, 'base_branch' : 'main', 'base_bench' : '',
        'base_network' : '', 'base_options' : 'Threads=1 Hash=16', 'base_time_control' : '8.0+0.08',
        'book_name' : 'UHO_Lichess_4852_v1.epd', 'upload_pgns' : 'FALSE', 'info' : '',
        'priority' : '0', 'throughput' : '1000', 'workload_size' : '32',
        'syzygy_wdl' : 'OPTIONAL', 'syzygy_adj' : 'OPTIONAL', 'win_adj' : 'None', 'draw_adj' : 'None',
        'scale_method' : 'BASE', 'scale_nps' : '1000000',
        **overrides,
    }

def test_fields(**overrides):
    return shared_fields(**{ 'test_mode' : 'SPRT', 'test_bounds' : '[0.00, 3.00]', 'test_confidence' : '[0.05, 0.05]', 'test_max_games' : '0', **overrides })

def tune_fields(**overrides):
    return shared_fields(**{
        'spsa_inputs' : 'Knight, int, 300, 200, 400, 10, 0.002',
        'spsa_reporting_type' : 'BATCHED', 'spsa_distribution_type' : 'SINGLE',
        'spsa_alpha' : '0.602', 'spsa_gamma' : '0.101', 'spsa_A_ratio' : '0.1',
        'spsa_iterations' : '1000', 'spsa_pairs_per' : '8',
        **overrides,
    })

class CreateWorkloadTests(TestCase):

    def setUp(self):
        create_engine_config()
        ensure_book()
        self.author = create_user('author')
        self.client.login(username='author', password=PASSWORD)

    def create(self, kind, fields):
        with mock.patch('requests.get', side_effect=github_commit):
            response = self.client.post('/%s/new/' % (kind), fields)
        self.assertEqual(response.status_code, 302)
        session = self.client.session
        error   = session.pop('error_message', None)
        session.save()
        return error

    def test_valid_test_is_created(self):
        self.assertIsNone(self.create('test', test_fields()))
        test = Test.objects.get()
        self.assertEqual((test.dev.bench, test.dev_time_control, test.throughput), (1234567, '8.0+0.08', 1000))
        self.assertFalse(test.approved)

    def test_unknown_upload_pgns_is_rejected(self):
        self.assertIn('Upload PGNs', self.create('test', test_fields(upload_pgns='SOMETIMES')))
        self.assertFalse(Test.objects.exists())
