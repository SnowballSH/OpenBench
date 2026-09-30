from django.test import TestCase

import OpenBench.stats
import OpenBench.views

from OpenBench.models import Machine, Result
from OpenBench.tests.fixtures import create_engine_config, create_test, create_user, ensure_book, system_info
from OpenBench.workloads.view_workload import fetch_result_summaries

class ResultSummaryTests(TestCase):

    def setUp(self):
        create_engine_config()
        ensure_book()
        self.test  = create_test(create_user('author'))
        self.owner = create_user('worker')

    def result(self, **fields):
        machine = Machine.objects.create(user=self.owner, info=system_info(cpu_name='Ryzen', isa_name='avx2'))
        return Result.objects.create(test=self.test, machine=machine, **fields)

    def test_rows_are_grouped_and_ordered(self):
        self.result(LL=1, LD=2, DD=3, DW=2, WW=1, dev_nodes=4000, dev_time=2000, dev_time_scaled=1000)
        self.result(LL=0, LD=1, DD=1, DW=1, WW=0)
        [row] = fetch_result_summaries(self.test)['cpu_name']
        self.assertEqual((row['key'], row['penta'], row['pairs'], row['percent']), ('Ryzen', '(1, 3, 4, 3, 1)', 12, '100.00'))
        self.assertEqual((row['dev_nps'], row['dev_nps_scaled'], row['base_nps']), (2000, 4000, 0))
        self.assertEqual(row['elo'], '0.00 ± %.2f' % ((lambda l, m, u: (u - l) / 2)(*OpenBench.stats.Elo((1, 3, 4, 3, 1)))))

    def test_nodes_without_time_do_not_divide_by_zero(self):
        self.result(DD=1, dev_nodes=500, dev_time=0, dev_time_scaled=0, base_nodes=500, base_time=1, base_time_scaled=0)
        [row] = fetch_result_summaries(self.test)['user']
        self.assertEqual((row['dev_nps'], row['dev_nps_scaled'], row['base_nps'], row['base_nps_scaled']), (0, 0, 500000, 0))

    def test_tunes_carry_no_elo(self):
        self.test.test_mode = 'SPSA'
        self.test.save()
        self.result(LL=1, LD=2, DD=3, DW=2, WW=1)
        for rows in fetch_result_summaries(self.test).values():
            self.assertEqual([row['pairs'] for row in rows], [9])
            self.assertNotIn('elo', rows[0])
