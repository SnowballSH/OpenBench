import dataclasses
from datetime import timedelta
from typing import Any, ClassVar
from unittest import mock

from django.db import connection
from django.test import TestCase
from django.utils import timezone

from OpenBench.config import OPENBENCH_CONFIG
from OpenBench.diagnosis.domain import DiagnosisState
from OpenBench.diagnosis.report import diagnose_active_workloads, diagnose_workload
from OpenBench.diagnosis.sources import load_fleet, worker_errors
from OpenBench.diagnosis.standing import FleetJudge, StandingKind
from OpenBench.models import LogEvent, Machine, Result, Test
from OpenBench.tests.datasets import SMALL, Dataset, DatasetSize, build_dataset
from OpenBench.tests.fixtures import (
    create_engine_config,
    create_test,
    create_user,
    credentials,
    ensure_book,
    present,
    register_payload,
)
from OpenBench.utils import get_active_tests

MEDIUM = dataclasses.replace(SMALL, users=8, tests=160, machines=400, results=1200, events=300)
DIAGNOSIS_QUERIES = 6


class ClientProtocolCase(TestCase):
    def setUp(self) -> None:
        create_engine_config()
        create_engine_config('Other')
        ensure_book()
        self.author = create_user('admin', approver=True)
        self.owner = create_user('lab-worker')

    def register(self, **info: Any) -> dict[str, Any]:
        session: dict[str, Any] = self.client.post('/clientWorkerInfo/', register_payload(self.owner, **info)).json()
        return session

    def request_workload(self, session: dict[str, Any], blacklist: tuple[int, ...] = ()) -> int | None:
        payload = {'machine_id': session['machine_id'], 'secret': session['secret'], 'blacklist': list(blacklist)}
        response = self.client.post('/clientGetWorkload/', payload).json()
        return response['workload']['test']['id'] if response else None

    def judge(self) -> FleetJudge:
        active = list(get_active_tests().order_by('id'))
        errors = worker_errors([workload.id for workload in active])
        return FleetJudge(load_fleet(active, errors, timezone.now()))

    def predicted(self, session: dict[str, Any]) -> set[int]:
        judge = self.judge()
        machine = Machine.objects.select_related('user').get(id=session['machine_id'])
        return {
            workload.id
            for workload in judge.fleet.active
            if not judge.obstacles(workload, machine)
            and present(judge.standing(workload, machine)).kind == StandingKind.NEXT
        }


class AgreementTests(ClientProtocolCase):
    def assert_agreement(self, session: dict[str, Any], expected: set[int], blacklist: tuple[int, ...] = ()) -> None:
        predicted = self.predicted(session)
        self.assertEqual(predicted, expected)
        assigned = self.request_workload(session, blacklist)
        self.assertEqual(assigned is None, not predicted)
        if assigned is not None:
            self.assertIn(assigned, predicted)

    def test_an_eligible_machine_gets_the_workload(self) -> None:
        test = create_test(self.author)
        self.assert_agreement(self.register(), {test.id})
        self.assertEqual(diagnose_workload(test).state, DiagnosisState.RUNNING)

    def test_ineligible_machines_get_nothing(self) -> None:
        cases: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {
            'cpu flag': ({}, {'cpu_flags': ['SSE2']}),
            'compiler': ({}, {'engines': []}),
            'operating system': ({}, {'os_name': 'Windows'}),
            'threads': ({'threads': 8}, {'concurrency': 4}),
            'tune threads': ({'threads': 3, 'test_mode': 'SPSA'}, {'concurrency': 4}),
            'thread odds': (
                {'threads': 3, 'base_options': 'Threads=1 Hash=16'},
                {'concurrency': 4, 'physical_cores': 2},
            ),
            'syzygy adjudication': ({'syzygy_adj': '6-MAN'}, {'syzygy_max': 5}),
            'syzygy probing': ({'syzygy_wdl': '3-MAN'}, {'syzygy_max': 2}),
            'noisy': ({}, {'noisy': True}),
            'only': ({}, {'engines': ['Avalanche', 'Other'], 'only': ['Other']}),
            'unapproved': ({'approved': False}, {}),
            'finished': ({'finished': True}, {}),
        }
        for name, (fields, info) in cases.items():
            with self.subTest(name):
                test = create_test(self.author, **fields)
                self.assert_agreement(self.register(**info), set())
                Test.objects.filter(id=test.id).delete()

    def test_eligible_counterparts_get_the_workload(self) -> None:
        cases: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {
            'threads': ({'threads': 4}, {'concurrency': 4}),
            'tune threads': ({'threads': 2, 'test_mode': 'SPSA'}, {'concurrency': 4}),
            'syzygy': ({'syzygy_adj': '5-MAN', 'syzygy_wdl': '5-MAN'}, {'syzygy_max': 5}),
            'noisy fixed nodes': ({'dev_time_control': 'N=25000', 'base_time_control': 'N=25000'}, {'noisy': True}),
            'only': ({}, {'engines': ['Avalanche', 'Other'], 'only': ['Avalanche']}),
            'focus elsewhere': ({}, {'engines': ['Avalanche', 'Other'], 'focus': ['Other']}),
        }
        for name, (fields, info) in cases.items():
            with self.subTest(name):
                test = create_test(self.author, **fields)
                if fields.get('test_mode') == 'SPSA':
                    self.skip_spsa_payload(test)
                    continue
                self.assert_agreement(self.register(**info), {test.id})
                Result.objects.filter(test=test).delete()
                Test.objects.filter(id=test.id).delete()

    def skip_spsa_payload(self, test: Test) -> None:
        # A tune without an SPSARun cannot be serialized for the Client, so only the prediction is compared
        session = self.register(concurrency=4)
        self.assertEqual(self.predicted(session), {test.id})
        Test.objects.filter(id=test.id).delete()

    def test_a_build_failure_blacklists_the_workload_for_that_session(self) -> None:
        test = create_test(self.author)
        session = self.register()
        self.assertEqual(self.request_workload(session), test.id)

        report = {**session, 'test_id': test.id, 'error': '[Avalanche] dev build failed', 'logs': 'zig: error'}
        with mock.patch('OpenBench.views.FileSystemStorage'):
            self.client.post('/clientSubmitError/', report)

        self.assert_agreement(session, set(), blacklist=(test.id,))
        self.assert_agreement(self.register(), {test.id})

    def test_priority_outranks(self) -> None:
        create_test(self.author, priority=0)
        urgent = create_test(self.author, priority=5)
        for _ in range(5):
            self.assert_agreement(self.register(), {urgent.id})

    def test_an_unplayable_priority_does_not_outrank(self) -> None:
        playable = create_test(self.author, priority=0)
        create_test(self.author, priority=5, threads=64)
        self.assert_agreement(self.register(), {playable.id})

    def test_focus_prefers_its_engine(self) -> None:
        create_test(self.author)
        other = create_test(self.author, engine='Other')
        self.assert_agreement(self.register(engines=['Avalanche', 'Other'], focus=['Other']), {other.id})

    def test_threads_follow_the_throughput_share(self) -> None:
        busy = create_test(self.author)
        idle = create_test(self.author)
        self.assertIn(self.request_workload(self.register(concurrency=64, physical_cores=64)), {busy.id, idle.id})
        taken = present(Machine.objects.exclude(workload=0).first()).workload
        for _ in range(3):
            self.assert_agreement(self.register(concurrency=1, physical_cores=1), {busy.id, idle.id} - {taken})

    def test_higher_throughput_is_served_first(self) -> None:
        create_test(self.author, throughput=100)
        favoured = create_test(self.author, throughput=10000)
        self.assert_agreement(self.register(), {favoured.id})

    def test_equal_shares_are_a_tie(self) -> None:
        tests = {create_test(self.author).id, create_test(self.author).id}
        self.assert_agreement(self.register(), tests)

    def test_balanced_engine_throughputs(self) -> None:
        avalanche = {create_test(self.author).id for _ in range(3)}
        other = create_test(self.author, engine='Other')
        engines = ['Avalanche', 'Other']
        with mock.patch.dict(OPENBENCH_CONFIG, {'balance_engine_throughputs': True}):
            self.assert_agreement(self.register(engines=engines), {other.id})
        Machine.objects.update(workload=0)
        with mock.patch.dict(OPENBENCH_CONFIG, {'balance_engine_throughputs': False}):
            self.assert_agreement(self.register(engines=engines), avalanche | {other.id})

    def test_machines_idle_for_longer_than_the_assignment_window_hold_no_threads(self) -> None:
        busy = create_test(self.author)
        idle = create_test(self.author)
        holder = self.register(concurrency=64, physical_cores=64)
        Machine.objects.filter(id=holder['machine_id']).update(
            workload=busy.id, updated=timezone.now() - timedelta(minutes=3)
        )
        self.assert_agreement(self.register(), {busy.id, idle.id})


class WorkloadDiagnosisTests(ClientProtocolCase):
    def age_machines(self, minutes: float) -> None:
        Machine.objects.update(updated=timezone.now() - timedelta(minutes=minutes))

    def test_states_follow_a_worker_through_a_workload(self) -> None:
        test = create_test(self.author)
        self.assertEqual(diagnose_workload(test).state, DiagnosisState.NO_WORKERS)

        session = self.register()
        self.assertEqual(diagnose_workload(test).state, DiagnosisState.WAITING)

        self.request_workload(session)
        self.assertEqual(diagnose_workload(test).state, DiagnosisState.RUNNING)

        self.age_machines(3)
        self.assertEqual(diagnose_workload(test).state, DiagnosisState.STARTING)

        Result.objects.update(updated=timezone.now() - timedelta(minutes=30))
        self.age_machines(30)
        self.assertEqual(diagnose_workload(test).state, DiagnosisState.NO_WORKERS)

    def test_a_reporting_worker_that_stops_producing_results_is_stalled(self) -> None:
        test = create_test(self.author)
        self.request_workload(self.register())
        Result.objects.update(games=20, updated=timezone.now() - timedelta(minutes=40))
        found = diagnose_workload(test)
        self.assertEqual(found.state, DiagnosisState.STALLED)
        self.assertIn('nothing has been reported for 40m', found.headline)

    def test_failed_workers_are_named(self) -> None:
        test = create_test(self.author)
        first = self.register()
        self.request_workload(first)
        LogEvent.objects.create(
            author='lab-worker', summary='Engine crashed', log_file='', machine_id=first['machine_id'], test_id=test.id
        )
        self.age_machines(5)
        Result.objects.update(updated=timezone.now() - timedelta(minutes=5))
        found = diagnose_workload(test)
        self.assertEqual(found.state, DiagnosisState.FAILING)
        self.assertIn('"Engine crashed"', found.headline)

    def test_stopped_workloads_report_the_error_that_preceded_the_stop(self) -> None:
        test = create_test(self.author, finished=True)
        LogEvent.objects.create(
            author='lab-worker', summary='Wrong Bench: 1', log_file='', machine_id=4, test_id=test.id
        )
        found = diagnose_workload(test)
        self.assertEqual(found.state, DiagnosisState.STOPPED_BY_ERROR)
        self.assertIn('Wrong Bench: 1', found.evidence[0].text)

    def test_only_ineligible_workers_online(self) -> None:
        test = create_test(self.author, threads=8)
        self.register(concurrency=4)
        found = diagnose_workload(test)
        self.assertEqual(found.state, DiagnosisState.NO_ELIGIBLE_WORKERS)
        self.assertIn('needs 8 threads', found.evidence[0].text)

    def test_outranked_names_the_workload_ahead(self) -> None:
        test = create_test(self.author)
        urgent = create_test(self.author, priority=3)
        self.register()
        found = diagnose_workload(test)
        self.assertEqual(found.state, DiagnosisState.OUTRANKED)
        self.assertEqual(present(found.evidence[0].link).href, f'/test/{urgent.id}/')


class DiagnosisPageTests(ClientProtocolCase):
    def setUp(self) -> None:
        super().setUp()
        self.client.force_login(self.author)

    def test_active_workload_page_explains_what_it_waits_for(self) -> None:
        test = create_test(self.author, threads=8)
        self.register(concurrency=4)
        content = self.client.get(f'/test/{test.id}/').content.decode()
        self.assertIn('class="diagnosis diagnosis-warning" data-diagnosis-state="no_eligible_workers"', content)
        self.assertIn('No eligible worker: none of the 1 kind of worker seen in the last 24h can take it.', content)
        self.assertIn('<details class="diagnosis-details" open>', content)

    def test_pending_workload_page_awaits_approval(self) -> None:
        test = create_test(self.author, approved=False)
        content = self.client.get(f'/test/{test.id}/').content.decode()
        self.assertIn('data-diagnosis-state="awaiting_approval"', content)

    def test_finished_workload_page_has_no_banner(self) -> None:
        test = create_test(self.author, finished=True, passed=True)
        self.assertNotIn('data-diagnosis-state', self.client.get(f'/test/{test.id}/').content.decode())

    def test_running_banner_is_calm_and_collapsed(self) -> None:
        test = create_test(self.author)
        self.request_workload(self.register())
        self.client.force_login(self.author)
        content = self.client.get(f'/test/{test.id}/').content.decode()
        self.assertIn('class="diagnosis diagnosis-ok" data-diagnosis-state="running"', content)
        self.assertIn('<details class="diagnosis-details">', content)
        self.assertIn('<p class="diagnosis-brief">Running on 1 worker</p>', content)
        self.assertNotRegex(content, r'<li>\s*</li>')

    def test_index_row_shows_the_reason_when_there_is_no_rate(self) -> None:
        create_test(self.author)
        create_test(self.author, priority=3)
        self.register()
        content = self.client.get('/index/').content.decode()
        self.assertIn('row-reason-warning', content)
        self.assertIn('outranked by priority 3', content)
        self.assertIn('next in line for a worker', content)

    def test_index_row_with_a_rate_shows_no_reason(self) -> None:
        test = create_test(self.author, games=4000, wins=1000, losses=1000, draws=2000)
        Test.objects.filter(id=test.id).update(creation=timezone.now() - timedelta(hours=2))
        content = self.client.get('/index/').content.decode()
        self.assertIn('games/h', content)
        self.assertNotIn('row-reason', content)


class DiagnosisBudgetTests(TestCase):
    size: ClassVar[DatasetSize] = SMALL
    data: ClassVar[Dataset]

    @classmethod
    def setUpTestData(cls) -> None:
        cls.data = build_dataset(cls.size)

    def test_every_active_workload_is_diagnosed_in_a_fixed_number_of_queries(self) -> None:
        with self.assertNumQueries(DIAGNOSIS_QUERIES):
            diagnoses = diagnose_active_workloads(timezone.now())
        self.assertEqual(set(diagnoses), set(get_active_tests().values_list('id', flat=True)))
        self.assertEqual(diagnoses[self.data.workload.id].state, DiagnosisState.RUNNING)

    def test_settled_workloads_cost_at_most_one_query(self) -> None:
        pending = next(test for test in self.data.tests if not test.approved and not test.finished)
        passed = next(test for test in self.data.tests if test.passed)
        stopped = next(test for test in self.data.tests if test.finished and not (test.passed or test.failed))
        for test, queries in ((pending, 0), (passed, 0), (stopped, 1)):
            with self.subTest(test=test.id), self.assertNumQueries(queries):
                diagnose_workload(test)


class LargerDiagnosisBudgetTests(DiagnosisBudgetTests):
    size = MEDIUM


class WorkloadsApiTests(ClientProtocolCase):
    def setUp(self) -> None:
        super().setUp()
        self.active = create_test(self.author, info='Pawn corrhist, STC\navl:6b10ec947ac0', currentllr=1.25)
        self.pending = create_test(self.author, approved=False)
        self.passed = create_test(self.author, finished=True, passed=True, games=4, wins=3, losses=1, DW=1, WW=1)
        self.other = create_test(self.author, engine='Other', deleted=True, finished=True)
        self.client.force_login(self.author)

    def ids(self, query: str = '') -> list[int]:
        return [row['id'] for row in self.client.get(f'/api/workloads/{query}').json()['workloads']]

    def test_requires_authentication(self) -> None:
        self.client.logout()
        self.assertEqual(self.client.get('/api/workloads/').status_code, 401)
        response = self.client.post('/api/workloads/?status=active', credentials(self.author))
        self.assertEqual([row['id'] for row in response.json()['workloads']], [self.active.id])

    def test_row_shape(self) -> None:
        payload = self.client.get('/api/workloads/?status=active').json()
        self.assertEqual(set(payload), {'workloads', 'count', 'next_since_id'})
        row = payload['workloads'][0]
        self.assertEqual(
            set(row),
            {
                'id',
                'mode',
                'status',
                'engine',
                'dev',
                'base',
                'time_control',
                'games',
                'llr',
                'llr_lower',
                'llr_upper',
                'elo',
                'created_at',
                'updated_at',
                'info',
                'diagnosis',
            },
        )
        self.assertEqual(row['dev'], {'name': 'dev', 'sha': 'a' * 40})
        self.assertEqual((row['mode'], row['status'], row['time_control']), ('SPRT', 'active', '8.0+0.08'))
        self.assertEqual((row['llr'], row['llr_lower'], row['llr_upper']), (1.25, -2.94, 2.94))
        self.assertEqual(row['info'], 'Pawn corrhist, STC')
        self.assertIsNone(row['elo'])
        self.assertEqual(row['diagnosis']['state'], 'no_workers')
        self.assertIn('No workers', row['diagnosis']['headline'])

    def test_status_filters(self) -> None:
        self.assertEqual(self.ids(), [self.other.id, self.passed.id, self.pending.id, self.active.id])
        self.assertEqual(self.ids('?status=all'), self.ids())
        self.assertEqual(self.ids('?status=pending'), [self.pending.id])
        self.assertEqual(self.ids('?status=finished'), [self.passed.id])

    def test_settled_rows_carry_their_state(self) -> None:
        rows = {row['id']: row for row in self.client.get('/api/workloads/').json()['workloads']}
        self.assertEqual(rows[self.pending.id]['diagnosis']['state'], 'awaiting_approval')
        self.assertEqual(rows[self.passed.id]['diagnosis']['state'], 'finished')
        self.assertEqual(rows[self.other.id]['status'], 'deleted')
        self.assertEqual(set(rows[self.passed.id]['elo']), {'lower', 'value', 'upper'})

    def test_engine_filter(self) -> None:
        self.assertEqual(self.ids('?engine=Other'), [self.other.id])
        self.assertEqual(self.ids('?engine=Missing'), [])

    def test_since_id_walks_forward(self) -> None:
        first = self.client.get('/api/workloads/?since_id=0&limit=3').json()
        self.assertEqual([row['id'] for row in first['workloads']], [self.active.id, self.pending.id, self.passed.id])
        self.assertEqual(first['next_since_id'], self.passed.id)
        rest = self.client.get(f'/api/workloads/?since_id={first["next_since_id"]}&limit=3').json()
        self.assertEqual([row['id'] for row in rest['workloads']], [self.other.id])
        self.assertIsNone(rest['next_since_id'])

    def test_limit_is_bounded(self) -> None:
        self.assertEqual(len(self.ids('?limit=2')), 2)
        for query in ('?limit=0', '?limit=201', '?limit=abc', '?since_id=-1', '?status=running'):
            with self.subTest(query):
                response = self.client.get(f'/api/workloads/{query}')
                self.assertEqual(response.status_code, 400)
                self.assertIn('error', response.json())

    def test_filters_in_a_post_body(self) -> None:
        response = self.client.post('/api/workloads/', {'status': 'pending'})
        self.assertEqual([row['id'] for row in response.json()['workloads']], [self.pending.id])

    def test_insights_carry_the_full_diagnosis(self) -> None:
        self.register(concurrency=4)
        Test.objects.filter(id=self.active.id).update(dev_options='Threads=8 Hash=16', base_options='Threads=8 Hash=16')
        self.client.force_login(self.author)
        diagnosis = self.client.get(f'/api/workload/{self.active.id}/insights/').json()['insights']['diagnosis']
        self.assertEqual(set(diagnosis), {'state', 'severity', 'headline', 'brief', 'evidence', 'urgent'})
        self.assertEqual((diagnosis['state'], diagnosis['severity']), ('no_eligible_workers', 'warning'))
        self.assertEqual(set(diagnosis['evidence'][0]), {'kind', 'text', 'link'})
        self.assertEqual(diagnosis['evidence'][0]['kind'], 'ineligible')
        self.assertEqual(set(diagnosis['evidence'][0]['link']), {'href', 'label'})


class OneShotWorkerTests(ClientProtocolCase):
    def one_shot(self) -> dict[str, Any]:
        return self.register(cli_options='-U lab-worker -T 4 --single_workload', machine_name='batch-job')

    def fail_build(self, session: dict[str, Any], test: Test) -> None:
        report = {**session, 'test_id': test.id, 'error': '[Avalanche] dev build failed', 'logs': 'zig: error'}
        with mock.patch('OpenBench.views.FileSystemStorage'):
            self.assertEqual(self.client.post('/clientSubmitError/', report).json(), {})

    def take_and_fail(self, test: Test) -> None:
        session = self.one_shot()
        self.assertEqual(self.request_workload(session), test.id)
        self.fail_build(session, test)

    def age(self, minutes: float) -> None:
        moment = timezone.now() - timedelta(minutes=minutes)
        Machine.objects.update(updated=moment)
        Result.objects.update(updated=moment)
        LogEvent.objects.update(created=moment)

    def test_a_job_that_failed_to_build_is_not_a_running_worker(self) -> None:
        test = create_test(self.author)
        self.take_and_fail(test)
        found = diagnose_workload(test)
        self.assertEqual(found.state, DiagnosisState.FAILING)
        self.assertIn('the last 1 worker to take it failed', found.headline)
        self.assertIn('"[Avalanche] dev build failed"', found.headline)

    def test_failed_jobs_do_not_become_ineligible_kinds_of_worker(self) -> None:
        test = create_test(self.author)
        for _ in range(3):
            self.take_and_fail(test)
        self.age(5)
        fresh = self.one_shot()

        found = diagnose_workload(test)
        self.assertEqual(found.state, DiagnosisState.FAILING)
        self.assertIn('the last 3 workers to take it all failed', found.headline)
        self.assertEqual(self.predicted(fresh), {test.id})
        self.assertEqual(self.request_workload(fresh), test.id)

    def test_old_build_failures_still_read_as_failing(self) -> None:
        test = create_test(self.author)
        self.take_and_fail(test)
        self.age(45)
        self.assertEqual(diagnose_workload(test).state, DiagnosisState.FAILING)

    def test_a_result_after_the_failures_resolves_them(self) -> None:
        test = create_test(self.author)
        self.take_and_fail(test)
        self.age(5)
        session = self.one_shot()
        self.request_workload(session)
        Result.objects.filter(machine_id=session['machine_id']).update(games=20, updated=timezone.now())
        self.assertEqual(diagnose_workload(test).state, DiagnosisState.RUNNING)

    def test_a_playing_worker_that_reports_a_crash_is_still_running(self) -> None:
        test = create_test(self.author)
        session = self.register()
        self.request_workload(session)
        report = {**session, 'test_id': test.id, 'error': 'Disconnect', 'logs': '[Event]'}
        with mock.patch('OpenBench.views.FileSystemStorage'):
            self.client.post('/clientSubmitError/', report)
        self.client.post('/clientHeartbeat/', {**session, 'test_id': test.id})
        self.assertEqual(diagnose_workload(test).state, DiagnosisState.RUNNING)

    def test_a_long_build_is_starting_and_not_an_idle_fleet(self) -> None:
        test = create_test(self.author)
        session = self.register()
        self.request_workload(session)
        self.age(12)
        found = diagnose_workload(test)
        self.assertEqual(found.state, DiagnosisState.STARTING)

        self.client.post('/clientHeartbeat/', {**session, 'test_id': test.id})
        self.assertEqual(diagnose_workload(test).state, DiagnosisState.RUNNING)

    def test_a_worker_silent_past_the_build_allowance_is_gone(self) -> None:
        test = create_test(self.author)
        self.request_workload(self.register())
        self.age(45)
        self.assertEqual(diagnose_workload(test).state, DiagnosisState.NO_WORKERS)

    def test_checking_in_without_a_result_past_the_allowance_is_stalled(self) -> None:
        test = create_test(self.author)
        session = self.register()
        self.request_workload(session)
        self.age(60)
        self.client.post('/clientHeartbeat/', {**session, 'test_id': test.id})
        found = diagnose_workload(test)
        self.assertEqual(found.state, DiagnosisState.STALLED)
        self.assertIn('1 worker (4 threads) still holds this workload and checks in', found.headline)


class StoppedByErrorTests(ClientProtocolCase):
    def setUp(self) -> None:
        super().setUp()
        self.test = create_test(self.author)
        session = self.register()
        self.request_workload(session)
        report = {**session, 'test_id': self.test.id, 'error': 'Wrong Bench: 123 (expected 1)'}
        self.assertEqual(self.client.post('/clientBenchError/', report).json(), {})
        self.test.refresh_from_db()
        self.client.force_login(self.author)

    def test_bench_mismatch_stops_the_workload_and_says_so(self) -> None:
        self.assertTrue(self.test.finished)
        found = diagnose_workload(self.test)
        self.assertEqual(found.state, DiagnosisState.STOPPED_BY_ERROR)
        self.assertEqual(found.headline, 'Stopped by a worker error: "Wrong Bench: 123 (expected 1)".')

    def test_workload_page_shows_the_banner(self) -> None:
        content = self.client.get(f'/test/{self.test.id}/').content.decode()
        self.assertIn('class="diagnosis diagnosis-warning" data-diagnosis-state="stopped_by_error"', content)
        self.assertIn('Stopped by a worker error: &quot;Wrong Bench: 123 (expected 1)&quot;.', content)

    def test_listings_say_why(self) -> None:
        row = self.client.get('/api/workloads/').json()['workloads'][0]
        self.assertEqual((row['status'], row['diagnosis']['state']), ('stopped', 'stopped_by_error'))
        self.assertIn('Wrong Bench', row['diagnosis']['headline'])
        self.assertIn('stopped by a worker error: Wrong Bench: 123', self.client.get('/index/').content.decode())

    def test_a_later_manual_action_is_the_cause_instead(self) -> None:
        LogEvent.objects.create(author='admin', summary='STOP', log_file='', test_id=self.test.id)
        self.assertEqual(diagnose_workload(self.test).state, DiagnosisState.FINISHED)
        self.assertNotIn('data-diagnosis-state', self.client.get(f'/test/{self.test.id}/').content.decode())


class DegenerateWorkloadTests(ClientProtocolCase):
    def test_zero_throughput_is_unknown_instead_of_a_server_error(self) -> None:
        broken = create_test(self.author, throughput=0)
        other = create_test(self.author)
        self.register()
        self.client.force_login(self.author)
        for url in ('/index/', f'/test/{broken.id}/', f'/test/{other.id}/', '/api/workloads/?status=active'):
            with self.subTest(url):
                self.assertEqual(self.client.get(url).status_code, 200)
        self.assertEqual(diagnose_workload(broken).state, DiagnosisState.UNKNOWN)

    def test_worker_errors_are_found_through_the_index(self) -> None:
        with connection.cursor() as cursor:
            cursor.execute(
                'EXPLAIN QUERY PLAN SELECT id FROM "OpenBench_logevent" '
                'WHERE test_id IN (1, 2) AND machine_id > 0 ORDER BY id DESC LIMIT 200'
            )
            plan = ' '.join(str(row[-1]) for row in cursor.fetchall())
        self.assertIn('logevent_test_machine', plan)
