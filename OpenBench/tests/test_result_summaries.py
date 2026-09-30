from django.test import TestCase

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

    def test_nodes_without_time_do_not_divide_by_zero(self):
        self.result(DD=1, dev_nodes=500, dev_time=0, dev_time_scaled=0, base_nodes=500, base_time=1, base_time_scaled=0)
        [row] = fetch_result_summaries(self.test)['user']
        self.assertEqual((row['dev_nps'], row['dev_nps_scaled'], row['base_nps'], row['base_nps_scaled']), (0, 0, 500000, 0))
