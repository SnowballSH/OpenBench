import numpy as np

from django.test import TestCase

from OpenBench.models import SPSAParameter, SPSARun
from OpenBench.spsa_utils import spsa_optimal_values, spsa_original_input, spsa_param_digest, spsa_workload_assignment_dict
from OpenBench.tests.fixtures import create_engine_config, create_test, create_user, ensure_book

class SPSAUtilsTests(TestCase):

    def setUp(self):
        create_engine_config()
        ensure_book()
        self.test = create_test(create_user('author'), test_mode='SPSA', workload_size=8)

    def tune(self, distribution='SINGLE', params=()):
        spsa_run = SPSARun.objects.create(tune=self.test, iterations=1000, pairs_per=8, alpha=0.602, gamma=0.101,
            a_ratio=0.1, reporting_type='BATCHED', distribution_type=distribution)
        for index, (name, is_float, value, low, high, c_end, r_end) in enumerate(params):
            c_value = c_end * 1000 ** 0.101
            a_value = r_end * c_end ** 2 * (0.1 * 1000 + 1000) ** 0.602
            SPSAParameter.objects.create(spsa_run=spsa_run, name=name, index=index, value=value, is_float=is_float,
                start=value, min_value=low, max_value=high, c_end=c_end, r_end=r_end, c_value=c_value, a_value=a_value)
        self.test.refresh_from_db()
        return spsa_run

    def test_non_spsa_workloads_have_no_assignment(self):
        self.test.test_mode = 'SPRT'
        self.assertIsNone(spsa_workload_assignment_dict(self.test, 4))

    def test_single_distribution_duplicates_one_permutation(self):
        self.tune('SINGLE', [('Knight', False, 300, 200, 400, 10, 0.002), ('Scale', True, 1.5, 1.0, 2.0, 0.1, 0.002)])
        assignment = spsa_workload_assignment_dict(self.test, 4)
        self.assertEqual([assignment[name]['index'] for name in ('Knight', 'Scale')], [0, 1])
        for data in assignment.values():
            self.assertEqual(len(data['dev']), 4)
            self.assertEqual(len(set(data['dev'])), 1)
            self.assertEqual(len(set(data['flip'])), 1)

    def test_multiple_distribution_draws_a_permutation_per_runner(self):
        self.tune('MULTIPLE', [('Knight', False, 300, 200, 400, 10, 0.002)])
        np.random.seed(1)
        knight = spsa_workload_assignment_dict(self.test, 64)['Knight']
        self.assertEqual(len(knight['dev']), 64)
        self.assertEqual(set(knight['flip']), { -1, 1 })

    def test_perturbations_respect_types_and_bounds(self):
        self.tune('MULTIPLE', [('Knight', False, 399, 200, 400, 10, 0.002), ('Scale', True, 1.0, 1.0, 2.0, 0.1, 0.002)])
        assignment = spsa_workload_assignment_dict(self.test, 32)
        for dev, base, flip in zip(assignment['Knight']['dev'], assignment['Knight']['base'], assignment['Knight']['flip']):
            self.assertIsInstance(dev, int)
            self.assertTrue(200 <= dev <= 400 and 200 <= base <= 400)
            self.assertEqual(flip > 0, dev >= base)
        for dev, base in zip(assignment['Scale']['dev'], assignment['Scale']['base']):
            self.assertIsInstance(dev, float)
            self.assertTrue(1.0 <= dev <= 2.0 and 1.0 <= base <= 2.0)

    def test_integer_perturbations_are_at_least_half_a_step(self):
        self.tune('SINGLE', [('Tiny', False, 300, 200, 400, 0.01, 0.002)])
        self.assertEqual(spsa_workload_assignment_dict(self.test, 1)['Tiny']['c'], 0.5)

    def test_c_and_r_reach_their_end_values_on_the_last_iteration(self):
        self.tune('SINGLE', [('Scale', True, 1.5, 1.0, 2.0, 0.1, 0.002)])
        self.test.games = 2 * 8 * 999
        scale = spsa_workload_assignment_dict(self.test, 1)['Scale']
        self.assertAlmostEqual(scale['c'], 0.1, places=9)
        self.assertAlmostEqual(scale['r'], 0.002, places=9)

    def test_digest_matches_the_assignment(self):
        self.tune('SINGLE', [('Scale', True, 1.5, 1.0, 2.0, 0.1, 0.002)])
        row = spsa_param_digest(self.test).split('\n')[1].split(',')
        scale = spsa_workload_assignment_dict(self.test, 1)['Scale']
        self.assertEqual((row[5], row[7]), ('%.4f' % scale['c'], '%.4f' % scale['r']))

    def test_original_input_and_optimal_values(self):
        self.tune('SINGLE', [('Knight', False, 300, 200, 400, 10, 0.002), ('Scale', True, 1.5, 1.0, 2.0, 0.1, 0.002)])
        self.assertEqual(spsa_original_input(self.test), 'Knight, int, 300.0, 200.0, 400.0, 10.0, 0.002\nScale, float, 1.5, 1.0, 2.0, 0.1, 0.002')
        self.assertEqual(spsa_optimal_values(self.test), 'Knight, 300\nScale, 1.5')
