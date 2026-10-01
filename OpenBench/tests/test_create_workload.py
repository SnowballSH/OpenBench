import html
import re
from unittest import mock

from django.test import TestCase

from OpenBench.models import SPSARun, Test
from OpenBench.tests.fixtures import PASSWORD, create_engine_config, create_user, ensure_book

REPO = 'https://github.com/SnowballSH/Avalanche'


def github_commit(url, **kwargs):
    commit = {'sha': 'c' * 40, 'commit': {'message': 'Change things\n\nBench: 1234567'}}
    # GitHub answers /commits/<sha> with the commit itself and /branches/<name> with it nested
    return mock.Mock(**{'json.return_value': commit if '/commits/' in url else {'commit': commit}})


def github_listing(url, **kwargs):
    # GitHub answers a nameless /branches or /commits with a list of them
    if url.endswith(('/branches', '/commits')):
        return mock.Mock(**{'json.return_value': [{'name': 'main', 'commit': {'sha': 'c' * 40}}]})
    return github_commit(url, **kwargs)


def rendered_error(response):
    match = re.search(r'class="error-message"[^>]*>\s*<pre>(.*?)</pre>', response.content.decode(), re.DOTALL)
    return html.unescape(match.group(1)) if match else None


def shared_fields(**overrides):
    return {
        'dev_engine': 'Avalanche',
        'dev_repo': REPO,
        'dev_branch': 'dev',
        'dev_bench': '',
        'dev_network': '',
        'dev_options': 'Threads=1 Hash=16',
        'dev_time_control': '8.0+0.08',
        'base_engine': 'Avalanche',
        'base_repo': REPO,
        'base_branch': 'main',
        'base_bench': '',
        'base_network': '',
        'base_options': 'Threads=1 Hash=16',
        'base_time_control': '8.0+0.08',
        'book_name': 'UHO_Lichess_4852_v1.epd',
        'upload_pgns': 'FALSE',
        'info': '',
        'priority': '0',
        'throughput': '1000',
        'workload_size': '32',
        'syzygy_wdl': 'OPTIONAL',
        'syzygy_adj': 'OPTIONAL',
        'win_adj': 'None',
        'draw_adj': 'None',
        'scale_method': 'BASE',
        'scale_nps': '1000000',
        **overrides,
    }


def test_fields(**overrides):
    return shared_fields(
        **{
            'test_mode': 'SPRT',
            'test_bounds': '[0.00, 3.00]',
            'test_confidence': '[0.05, 0.05]',
            'test_max_games': '0',
            **overrides,
        }
    )


def tune_fields(**overrides):
    return shared_fields(
        **{
            'spsa_inputs': 'Knight, int, 300, 200, 400, 10, 0.002',
            'spsa_reporting_type': 'BATCHED',
            'spsa_distribution_type': 'SINGLE',
            'spsa_alpha': '0.602',
            'spsa_gamma': '0.101',
            'spsa_A_ratio': '0.1',
            'spsa_iterations': '1000',
            'spsa_pairs_per': '8',
            **overrides,
        }
    )


def datagen_fields(**overrides):
    return shared_fields(
        **{
            'datagen_max_games': '1000',
            'datagen_custom_genfens': '',
            'datagen_play_reverses': 'NO',
            **overrides,
        }
    )


class CreateWorkloadTests(TestCase):
    def setUp(self):
        create_engine_config()
        ensure_book()
        self.author = create_user('author')
        self.client.login(username='author', password=PASSWORD)

    def create(self, kind, fields, github=github_commit):
        with mock.patch('requests.get', side_effect=github):
            response = self.client.post(f'/{kind}/new/', fields)
        if response.status_code == 200:
            self.assertIsNotNone(error := rendered_error(response))
            return error
        self.assertEqual(response.status_code, 302)
        session = self.client.session
        error = session.pop('error_message', None)
        session.save()
        return error

    def test_bench_fields_submit_empty_so_the_commit_bench_is_used(self):
        content = self.client.get('/test/new/').content.decode()
        self.assertNotIn('value="Autofill"', content)
        self.assertEqual(content.count('placeholder="Autofill"'), 2)

    def test_valid_test_is_created(self):
        self.assertIsNone(self.create('test', test_fields()))
        test = Test.objects.get()
        self.assertEqual((test.dev.bench, test.dev_time_control, test.throughput), (1234567, '8.0+0.08', 1000))
        self.assertFalse(test.approved)

    def test_unknown_upload_pgns_is_rejected(self):
        self.assertIn('Upload PGNs', self.create('test', test_fields(upload_pgns='SOMETIMES')))
        self.assertFalse(Test.objects.exists())

    def test_fractional_throughput_is_rejected(self):
        self.assertIn('Throughput', self.create('test', test_fields(throughput='2.5')))
        self.assertFalse(Test.objects.exists())

    def test_fractional_throughput_is_rejected_for_tunes(self):
        self.assertIn('Throughput', self.create('tune', tune_fields(throughput='2.5')))
        self.assertFalse(Test.objects.exists())

    def test_out_of_range_integers_are_rejected(self):
        for field in ['priority', 'throughput', 'workload_size', 'scale_nps']:
            error = self.create('test', test_fields(**{field: str(10**30)}))
            self.assertIsNotNone(error, field)
        self.assertFalse(Test.objects.exists())

    def test_unknown_engine_is_an_error_not_a_crash(self):
        self.assertIn('Dev Engine was not found', self.create('test', test_fields(dev_engine='Nonexistent')))
        self.assertFalse(Test.objects.exists())

    def test_sprt_bounds_outside_the_pentanomial_domain_are_rejected(self):
        self.assertIn('SPRT Bounds', self.create('test', test_fields(test_bounds='[0.00, 230.01]')))
        self.assertIn('SPRT Bounds', self.create('test', test_fields(test_bounds='[-230.01, 0.00]')))
        self.assertIsNone(self.create('test', test_fields(test_bounds='[-229.99, 229.99]')))

    def test_valid_tune_is_created(self):
        self.assertIsNone(self.create('tune', tune_fields()))
        self.assertEqual(SPSARun.objects.get().a_ratio, 0.1)

    def test_tune_form_without_info_is_created(self):
        fields = {name: value for name, value in tune_fields().items() if not name.startswith('base_')}
        self.assertIsNone(self.create('tune', fields))
        self.assertEqual(Test.objects.get().info, '')

    def test_malformed_a_ratio_is_an_error_not_a_crash(self):
        for value in ['abc', '-1']:
            self.assertIn('A-Ratio', self.create('tune', tune_fields(spsa_A_ratio=value)))
        self.assertFalse(Test.objects.exists())

    def test_duplicate_spsa_parameter_names_are_rejected(self):
        inputs = 'Knight, int, 300, 200, 400, 10, 0.002\nKnight, int, 500, 400, 600, 10, 0.002'
        self.assertIn('unique', self.create('tune', tune_fields(spsa_inputs=inputs)))
        self.assertFalse(Test.objects.exists())

    def test_unnamed_spsa_parameters_are_rejected(self):
        self.assertIn('name', self.create('tune', tune_fields(spsa_inputs=' , int, 300, 200, 400, 10, 0.002')))
        self.assertFalse(Test.objects.exists())

    def test_spsa_parameters_keep_their_order(self):
        inputs = 'Knight, int, 300, 200, 400, 10, 0.002\r\nBishop, float, 3.5, 3.0, 4.0, 0.1, 0.002'
        self.assertIsNone(self.create('tune', tune_fields(spsa_inputs=inputs)))
        params = SPSARun.objects.get().parameters.order_by('index')
        self.assertEqual(
            [(p.name, p.is_float, p.value) for p in params], [('Knight', False, 300.0), ('Bishop', True, 3.5)]
        )

    def test_github_requests_are_bounded(self):
        with mock.patch('requests.get', side_effect=github_commit) as get:
            self.client.post('/test/new/', test_fields())
        self.assertEqual(get.call_count, 3)
        self.assertTrue(all(call.kwargs.get('timeout') for call in get.call_args_list))

    def test_supplied_bench_is_used(self):
        self.assertIsNone(self.create('test', test_fields(dev_bench='7654321')))
        self.assertEqual(Test.objects.get().dev.bench, 7654321)

    def test_unusable_supplied_bench_is_an_error(self):
        for bench in [str(2**31), 'abc']:
            self.assertIn('Bench for dev', self.create('test', test_fields(dev_bench=bench)))
        self.assertFalse(Test.objects.exists())

    def test_empty_branch_is_rejected_before_asking_github(self):
        for kind, fields in (('datagen', datagen_fields), ('test', test_fields)):
            github = mock.Mock(side_effect=github_listing)
            with self.subTest(kind=kind), mock.patch('traceback.print_exc') as print_exc:
                self.assertIn('Base Branch is required', self.create(kind, fields(base_branch=''), github))
                print_exc.assert_not_called()
                requested = [call.args[0] for call in github.call_args_list]
                self.assertFalse([url for url in requested if url.endswith(('/branches', '/commits'))])
        self.assertFalse(Test.objects.exists())
