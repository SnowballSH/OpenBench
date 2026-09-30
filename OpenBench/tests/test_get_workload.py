from unittest import mock

from django.test import RequestFactory, TestCase

import OpenBench.utils  # noqa: F401 - must load before get_workload to resolve the upstream import cycle
from OpenBench.config import OPENBENCH_CONFIG
from OpenBench.models import Machine, SPSARun
from OpenBench.tests.fixtures import create_engine_config, create_test, create_user, ensure_book, system_info
from OpenBench.workloads.get_workload import game_distribution, get_workload, select_workload


def machine_info(**overrides):
    return {**system_info(), 'supported': ['Avalanche'], **overrides}


class AssignmentTests(TestCase):
    def setUp(self):
        create_engine_config()
        ensure_book()
        self.author = create_user('author')
        self.owner = create_user('worker')

    def machine(self, **info):
        return Machine.objects.create(user=self.owner, info=machine_info(**info))

    def select(self, machine, blacklist=()):
        request = RequestFactory().post('/clientGetWorkload/', {'blacklist': list(blacklist)})
        return select_workload(request, machine)

    def test_no_active_workloads(self):
        self.assertIsNone(self.select(self.machine()))

    def test_unapproved_finished_and_deleted_workloads_are_skipped(self):
        create_test(self.author, approved=False)
        create_test(self.author, finished=True)
        create_test(self.author, deleted=True)
        self.assertIsNone(self.select(self.machine()))

    def test_unsupported_engines_are_skipped(self):
        create_test(self.author)
        self.assertIsNone(self.select(self.machine(supported=[])))

    def test_insufficient_threads(self):
        create_test(self.author, threads=8)
        self.assertIsNone(self.select(self.machine(concurrency=4)))

    def test_tunes_need_threads_for_a_pair(self):
        create_test(self.author, threads=4, test_mode='SPSA')
        self.assertIsNone(self.select(self.machine(concurrency=6, physical_cores=6)))
        self.assertIsNotNone(self.select(self.machine(concurrency=8, physical_cores=8)))

    def test_core_odds_ignore_hyperthreads(self):
        test = create_test(self.author, threads=4)
        test.base_options = 'Threads=2 Hash=16'
        test.save()
        self.assertIsNone(self.select(self.machine(concurrency=6, physical_cores=3)))
        self.assertEqual(self.select(self.machine(concurrency=8, physical_cores=4)), test)

    def test_syzygy_requirements(self):
        create_test(self.author, syzygy_adj='6-MAN')
        wdl = create_test(self.author, syzygy_wdl='5-MAN')
        self.assertIsNone(self.select(self.machine(syzygy_max=4)))
        self.assertEqual(self.select(self.machine(syzygy_max=5)), wdl)

    def test_blacklist(self):
        test = create_test(self.author)
        self.assertIsNone(self.select(self.machine(), blacklist=[test.id]))

    def test_noisy_machines_only_take_fixed_node_or_depth_workloads(self):
        create_test(self.author)
        nodes = create_test(self.author, dev_time_control='N=25000', base_time_control='N=25000')
        machine = self.machine(noisy=True)
        for _ in range(10):
            self.assertEqual(self.select(machine), nodes)

    def test_highest_priority_wins(self):
        create_test(self.author, priority=1)
        urgent = create_test(self.author, priority=5)
        for _ in range(10):
            self.assertEqual(self.select(self.machine()), urgent)

    def test_only_restricts_to_the_listed_engines(self):
        create_engine_config('Other')
        create_test(self.author, engine='Other', priority=9)
        mine = create_test(self.author)
        machine = self.machine(supported=['Avalanche', 'Other'], only=['Avalanche'])
        self.assertEqual(self.select(machine), mine)

    def test_focus_prefers_but_does_not_require(self):
        create_engine_config('Other')
        other = create_test(self.author, engine='Other')
        mine = create_test(self.author)
        machine = self.machine(supported=['Avalanche', 'Other'], focus=['Avalanche'])
        for _ in range(10):
            self.assertEqual(self.select(machine), mine)
        mine.finished = True
        mine.save()
        self.assertEqual(self.select(machine), other)

    def test_threads_are_spread_by_throughput(self):
        busy = create_test(self.author)
        idle = create_test(self.author)
        Machine.objects.create(user=self.owner, info=machine_info(concurrency=64), workload=busy.id)
        for _ in range(10):
            self.assertEqual(self.select(self.machine()), idle)

    def test_machines_keep_a_workload_within_the_fairness_margin(self):
        first = create_test(self.author)
        second = create_test(self.author)
        for workload in (first, second):
            Machine.objects.create(user=self.owner, info=machine_info(concurrency=8), workload=workload.id)
        for workload in (first, second):
            machine = self.machine(concurrency=4)
            machine.workload = workload.id
            for _ in range(10):
                self.assertEqual(self.select(machine), workload)

    def test_machines_leave_a_workload_beyond_the_fairness_margin(self):
        crowded = create_test(self.author)
        empty = create_test(self.author)
        Machine.objects.create(user=self.owner, info=machine_info(concurrency=16), workload=crowded.id)
        machine = self.machine(concurrency=4)
        machine.workload = crowded.id
        self.assertEqual(self.select(machine), empty)

    def test_balance_engine_throughputs(self):
        create_engine_config('Other')
        avalanche = [create_test(self.author) for _ in range(3)]
        other = create_test(self.author, engine='Other')
        Machine.objects.create(user=self.owner, info=machine_info(concurrency=8), workload=other.id)
        for workload in avalanche:
            Machine.objects.create(user=self.owner, info=machine_info(concurrency=4), workload=workload.id)
        machine = self.machine(supported=['Avalanche', 'Other'])
        with mock.patch.dict(OPENBENCH_CONFIG, {'balance_engine_throughputs': True}):
            self.assertEqual(self.select(machine), other)
        with mock.patch.dict(OPENBENCH_CONFIG, {'balance_engine_throughputs': False}):
            self.assertIn(self.select(machine), avalanche)


class DistributionTests(TestCase):
    def setUp(self):
        create_engine_config()
        ensure_book()
        self.author = create_user('author')
        self.owner = create_user('worker')

    def machine(self, **info):
        return Machine.objects.create(user=self.owner, info=machine_info(**info))

    def test_single_threaded(self):
        test = create_test(self.author, workload_size=32)
        self.assertEqual(
            game_distribution(test, self.machine(concurrency=8, physical_cores=8)),
            {'runner-count': 1, 'concurrency-per': 8, 'rounds-per-runner': 512},
        )

    def test_sockets_split_single_threaded_games(self):
        test = create_test(self.author, workload_size=32)
        self.assertEqual(
            game_distribution(test, self.machine(concurrency=8, physical_cores=8, sockets=2)),
            {'runner-count': 2, 'concurrency-per': 4, 'rounds-per-runner': 256},
        )

    def test_sockets_are_ignored_for_multi_threaded_games(self):
        test = create_test(self.author, threads=2, workload_size=10)
        self.assertEqual(
            game_distribution(test, self.machine(concurrency=8, physical_cores=8, sockets=2)),
            {'runner-count': 1, 'concurrency-per': 4, 'rounds-per-runner': 80},
        )

    def test_multiple_spsa(self):
        test = create_test(self.author, threads=1, test_mode='SPSA', workload_size=8)
        SPSARun.objects.create(
            tune=test,
            iterations=100,
            pairs_per=8,
            alpha=0.602,
            gamma=0.101,
            a_ratio=0.1,
            reporting_type='BATCHED',
            distribution_type='MULTIPLE',
        )
        self.assertEqual(
            game_distribution(test, self.machine(concurrency=8, physical_cores=8)),
            {'runner-count': 4, 'concurrency-per': 2, 'rounds-per-runner': 16},
        )

    def test_assignment_advances_the_book_index(self):
        test = create_test(self.author, workload_size=32)
        machine = self.machine(concurrency=8, physical_cores=8)
        request = RequestFactory().post('/clientGetWorkload/')
        first = get_workload(request, machine)['workload']
        second = get_workload(request, machine)['workload']
        self.assertEqual(first['test']['book_index'] + 256, second['test']['book_index'])
        machine.refresh_from_db()
        self.assertEqual(machine.workload, test.id)
