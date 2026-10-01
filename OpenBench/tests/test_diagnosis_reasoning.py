from datetime import UTC, datetime, timedelta
from typing import Any

from django.contrib.auth.models import User
from django.test import SimpleTestCase

from OpenBench.diagnosis.domain import (
    Activity,
    Diagnosis,
    DiagnosisState,
    EvidenceKind,
    Fleet,
    Link,
    ObstacleKind,
    Preparing,
    Severity,
)
from OpenBench.diagnosis.eligibility import obstacles
from OpenBench.diagnosis.fleet import group_workers
from OpenBench.diagnosis.reasoning import diagnose
from OpenBench.diagnosis.standing import FleetJudge
from OpenBench.models import Engine, EngineConfig, LogEvent, Machine, SPSARun, Test
from OpenBench.tests.fixtures import system_info

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
OWNER = User(id=1, username='lab-worker')


def minutes_ago(minutes: float) -> datetime:
    return NOW - timedelta(minutes=minutes)


def engine_config(name: str = 'Avalanche', **fields: Any) -> EngineConfig:
    values = {'build_compilers': 'zig>=0.16.0', 'build_cpuflags': 'AVX2', 'build_systems': 'Linux Darwin', **fields}
    return EngineConfig(name=name, **values)


CONFIGS = {'Avalanche': engine_config(), 'Other': engine_config('Other')}


def workload(id: int = 10, engine: str = 'Avalanche', threads: int = 1, **fields: Any) -> Test:
    options = f'Threads={threads} Hash=16'
    values = {
        'dev': Engine(name='dev'),
        'base': Engine(name='base'),
        'dev_engine': engine,
        'base_engine': engine,
        'dev_options': options,
        'base_options': options,
        'dev_time_control': '8.0+0.08',
        'base_time_control': '8.0+0.08',
        'approved': True,
        'throughput': 1000,
        **fields,
    }
    return Test(id=id, **values)


def machine(id: int = 1, seen: float = 1, holds: int = 0, **info: Any) -> Machine:
    details = {**system_info(), 'cpu_name': 'AMD EPYC 9R14', 'supported': ['Avalanche'], **info}
    return Machine(id=id, user=OWNER, info=details, updated=minutes_ago(seen), workload=holds)


def error(machine_id: int, summary: str, minutes: float, test_id: int = 10, log_file: str = '') -> LogEvent:
    return LogEvent(
        id=machine_id,
        machine_id=machine_id,
        test_id=test_id,
        summary=summary,
        created=minutes_ago(minutes),
        log_file=log_file,
    )


def fleet(
    machines: list[Machine], active: list[Test], failures: frozenset[tuple[int, int]] = frozenset()
) -> FleetJudge:
    newest_first = sorted(machines, key=lambda item: item.updated, reverse=True)
    return FleetJudge(
        Fleet(
            active=tuple(active),
            groups=tuple(group_workers(newest_first, NOW, {machine_id for _, machine_id in failures})),
            assigned=tuple(item for item in newest_first if NOW - item.updated <= timedelta(minutes=2)),
            last_seen=newest_first[0] if newest_first else None,
            configs=CONFIGS,
            build_failures=failures,
        )
    )


def kinds(diagnosis: Diagnosis) -> list[EvidenceKind]:
    return [item.kind for item in diagnosis.evidence]


class SettledWorkloadTests(SimpleTestCase):
    def test_pending_awaits_approval(self):
        found = diagnose(workload(approved=False), fleet([], []), Activity(), NOW)
        self.assertEqual(found.state, DiagnosisState.AWAITING_APPROVAL)
        self.assertEqual(found.severity, Severity.INFO)

    def test_finished_stopped_and_deleted_are_not_applicable(self):
        cases: tuple[tuple[dict[str, Any], str], ...] = (
            ({'finished': True, 'passed': True}, 'has finished'),
            ({'finished': True}, 'was stopped'),
            ({'deleted': True}, 'is deleted'),
        )
        for fields, text in cases:
            with self.subTest(fields=fields):
                found = diagnose(workload(**fields), fleet([machine()], []), Activity(), NOW)
                self.assertEqual(found.state, DiagnosisState.FINISHED)
                self.assertIn(text, found.headline)

    def test_completed_tune_has_finished(self):
        tune = workload(test_mode='SPSA', finished=True, games=1600)
        tune.spsa_run = SPSARun(pairs_per=8, iterations=100)
        self.assertIn('has finished', diagnose(tune, fleet([], []), Activity(), NOW).headline)

    def test_stopped_workload_cites_its_last_worker_error(self):
        activity = Activity(errors=(error(7, 'Wrong Bench: 123', 30),))
        found = diagnose(workload(finished=True), fleet([], []), activity, NOW)
        self.assertEqual(kinds(found), [EvidenceKind.ERROR])
        self.assertIn('Wrong Bench: 123', found.evidence[0].text)


class NoWorkerTests(SimpleTestCase):
    def test_no_worker_has_ever_registered(self):
        test = workload()
        found = diagnose(test, fleet([], [test]), Activity(), NOW)
        self.assertEqual(found.state, DiagnosisState.NO_WORKERS)
        self.assertIn('No worker has ever registered', found.headline)
        self.assertEqual(found.severity, Severity.WARNING)

    def test_idle_fleet_says_when_the_last_worker_was_seen(self):
        test = workload()
        found = diagnose(test, fleet([machine(seen=185)], [test]), Activity(), NOW)
        self.assertEqual(found.state, DiagnosisState.NO_WORKERS)
        self.assertIn('The last worker was seen 3h 5m ago', found.headline)
        self.assertEqual(found.brief, 'no workers for 3h 5m')
        self.assertEqual(found.evidence[0].link, Link('/machines/1/', 'Machine 1'))
        self.assertIn(EvidenceKind.ELIGIBLE, kinds(found))

    def test_a_fleet_older_than_a_day_is_only_the_last_worker(self):
        test = workload()
        found = diagnose(test, fleet([machine(seen=60 * 30)], [test]), Activity(), NOW)
        self.assertEqual(found.state, DiagnosisState.NO_WORKERS)
        self.assertNotIn(EvidenceKind.ELIGIBLE, kinds(found))


class IneligibleWorkerTests(SimpleTestCase):
    def blocked(self, test: Test, worker: Machine, failures: frozenset[tuple[int, int]] = frozenset()) -> Diagnosis:
        found = diagnose(test, fleet([worker], [test], failures), Activity(), NOW)
        self.assertEqual(found.state, DiagnosisState.NO_ELIGIBLE_WORKERS)
        self.assertEqual(found.evidence[0].kind, EvidenceKind.INELIGIBLE)
        return found

    def reasons(self, test: Test, worker: Machine) -> list[ObstacleKind]:
        return [obstacle.kind for obstacle in obstacles(test, worker, CONFIGS, frozenset())]

    def test_missing_cpu_flag(self):
        found = self.blocked(workload(), machine(supported=[], cpu_flags=['SSE2']))
        self.assertIn('cannot build Avalanche: lacks the CPU flag AVX2', found.evidence[0].text)
        self.assertEqual(found.brief, 'no eligible worker: they cannot build the engine')

    def test_missing_compiler(self):
        found = self.blocked(workload(), machine(supported=[], compilers={}))
        self.assertIn('has no compiler for it (zig>=0.16.0)', found.evidence[0].text)

    def test_missing_token_for_a_private_engine(self):
        configs = {'Avalanche': engine_config(private=True)}
        found = obstacles(workload(), machine(supported=[]), configs, frozenset())
        self.assertIn('has no Git token for the private engine Avalanche', found[0].detail)

    def test_unsupported_operating_system(self):
        found = self.blocked(workload(), machine(supported=[], os_name='Windows'))
        self.assertIn('runs Windows, which the engine does not build on', found.evidence[0].text)

    def test_registration_predates_the_engine(self):
        found = self.blocked(workload(), machine(supported=[]))
        self.assertIn('did not support Avalanche when it registered', found.evidence[0].text)

    def test_threads_do_not_fit(self):
        found = self.blocked(workload(threads=8), machine(concurrency=4))
        self.assertIn('needs 8 threads (Threads=8); the machine offers 4', found.evidence[0].text)
        self.assertEqual(found.brief, 'no eligible worker: they have too few threads')

    def test_tunes_need_threads_for_two_games(self):
        tune = workload(threads=4, test_mode='SPSA')
        self.assertEqual(self.reasons(tune, machine(concurrency=6, physical_cores=6)), [ObstacleKind.THREADS])
        self.assertEqual(self.reasons(tune, machine(concurrency=8, physical_cores=8)), [])

    def test_thread_odds_do_not_count_hyperthreads(self):
        odds = workload(threads=4, base_options='Threads=2 Hash=16')
        found = obstacles(odds, machine(concurrency=6, physical_cores=3), CONFIGS, frozenset())
        self.assertIn('the machine offers 3 (hyperthreads do not count for thread odds)', found[0].detail)

    def test_syzygy_requirement(self):
        found = self.blocked(workload(syzygy_wdl='6-MAN'), machine(syzygy_max=5))
        self.assertIn('needs 6-MAN Syzygy tablebases; the machine has up to 5-MAN', found.evidence[0].text)

    def test_noisy_machine_and_a_timed_workload(self):
        self.assertEqual(self.reasons(workload(), machine(noisy=True)), [ObstacleKind.NOISY])
        nodes = workload(dev_time_control='N=25000', base_time_control='N=25000')
        self.assertEqual(self.reasons(nodes, machine(noisy=True)), [])

    def test_only_excludes_other_engines(self):
        worker = machine(supported=['Avalanche', 'Other'], only=['Other'])
        found = self.blocked(workload(), worker)
        self.assertIn('runs with --only Other', found.evidence[0].text)

    def test_build_failure_blacklists_the_session_that_reported_it(self):
        test = workload()
        failures = frozenset({(test.id, 1)})
        found = self.blocked(test, machine(), failures)
        self.assertIn('reported a build failure for this workload', found.evidence[0].text)
        self.assertEqual(obstacles(test, machine(id=2), CONFIGS, failures), [])

    def test_every_obstacle_is_listed(self):
        worker = machine(supported=[], concurrency=2, noisy=True, syzygy_max=2)
        found = self.reasons(workload(threads=4, syzygy_adj='5-MAN'), worker)
        self.assertEqual(
            found,
            [ObstacleKind.ENGINE_UNSUPPORTED, ObstacleKind.SYZYGY, ObstacleKind.NOISY, ObstacleKind.THREADS],
        )

    def test_online_workers_cannot_but_an_absent_one_could(self):
        test = workload(threads=8)
        small, large = machine(1, seen=1, concurrency=4), machine(2, seen=90, concurrency=16, physical_cores=16)
        found = diagnose(test, fleet([small, large], [test]), Activity(), NOW)
        self.assertEqual(found.state, DiagnosisState.NO_ELIGIBLE_WORKERS)
        self.assertIn('the workers that can were last seen 1h 30m ago', found.headline)
        self.assertEqual(kinds(found)[:2], [EvidenceKind.INELIGIBLE, EvidenceKind.ELIGIBLE])

    def test_a_pool_of_batch_jobs_is_one_group(self):
        test = workload(threads=8)
        names = [f'batch-a074174{index}-7d81-49a2-b96e-4b14d4c304c3:0' for index in range(1, 6)]
        workers = [machine(index, seen=index, machine_name=name) for index, name in enumerate(names, 1)]
        found = diagnose(test, fleet(workers, [test]), Activity(), NOW)
        self.assertEqual(kinds(found).count(EvidenceKind.INELIGIBLE), 1)
        self.assertIn(
            'batch-* on AMD EPYC 9R14, 4 threads (lab-worker): 5 hosts, last seen 1m ago', found.evidence[0].text
        )

    def test_registrations_of_one_host_count_once(self):
        test = workload(threads=8)
        workers = [machine(index, seen=index) for index in range(1, 4)]
        for worker in workers:
            worker.host_key = 'same-host'
        found = diagnose(test, fleet(workers, [test]), Activity(), NOW)
        self.assertIn('AMD EPYC 9R14, 4 threads (lab-worker): 1 host, last seen 1m ago', found.evidence[0].text)

    def test_unreadable_registrations_are_unknown(self):
        test = workload()
        broken = Machine(id=1, user=OWNER, info={'cpu_name': 'Mystery'}, updated=minutes_ago(1))
        found = diagnose(test, fleet([broken], [test]), Activity(), NOW)
        self.assertEqual(found.state, DiagnosisState.UNKNOWN)

    def test_a_workload_without_a_thread_option_is_unknown(self):
        test = workload(dev_options='Hash=16')
        found = diagnose(test, fleet([machine()], [test]), Activity(), NOW)
        self.assertEqual(found.state, DiagnosisState.UNKNOWN)


class PassedOverTests(SimpleTestCase):
    def test_higher_priority_workloads_take_the_workers(self):
        test, urgent = workload(priority=0), workload(12, priority=5, info='Pawn corrhist, LTC\navl:6b10')
        found = diagnose(test, fleet([machine()], [test, urgent]), Activity(), NOW)
        self.assertEqual(found.state, DiagnosisState.OUTRANKED)
        self.assertEqual(found.brief, 'outranked by priority 5')
        self.assertEqual(found.evidence[0].kind, EvidenceKind.HIGHER_PRIORITY)
        self.assertEqual(found.evidence[0].link, Link('/test/12/', 'Workload 12'))
        self.assertIn("#12 (Pawn corrhist, LTC) has priority 5, 5 above this workload's 0", found.evidence[0].text)

    def test_a_higher_priority_workload_the_worker_cannot_take_does_not_outrank(self):
        test, urgent = workload(priority=0), workload(12, priority=5, threads=64)
        found = diagnose(test, fleet([machine()], [test, urgent]), Activity(), NOW)
        self.assertEqual(found.state, DiagnosisState.WAITING)

    def test_focus_prefers_another_engine(self):
        test, other = workload(), workload(12, engine='Other')
        worker = machine(supported=['Avalanche', 'Other'], focus=['Other'])
        found = diagnose(test, fleet([worker], [test, other]), Activity(), NOW)
        self.assertEqual(found.state, DiagnosisState.OUTRANKED)
        self.assertEqual(found.evidence[0].kind, EvidenceKind.FOCUS)
        self.assertEqual(found.brief, 'outranked by Other focus')

    def test_low_throughput_share(self):
        test, favoured = workload(throughput=100), workload(12, throughput=10000)
        found = diagnose(test, fleet([machine()], [test, favoured]), Activity(), NOW)
        self.assertEqual(found.state, DiagnosisState.LOW_SHARE)
        self.assertEqual(found.evidence[0].kind, EvidenceKind.THROUGHPUT_SHARE)
        self.assertIn('#12 has 0 threads for throughput 10000', found.evidence[0].text)

    def test_busy_rivals_leave_the_next_worker_to_the_idle_workload(self):
        test, busy = workload(), workload(12)
        workers = [machine(1), machine(2, holds=12, concurrency=64)]
        found = diagnose(test, fleet(workers, [test, busy]), Activity(), NOW)
        self.assertEqual(found.state, DiagnosisState.WAITING)

    def test_one_worker_that_would_take_it_is_enough(self):
        test, other = workload(), workload(12, engine='Other')
        focused = machine(1, supported=['Avalanche', 'Other'], focus=['Other'])
        found = diagnose(test, fleet([focused, machine(2)], [test, other]), Activity(), NOW)
        self.assertEqual(found.state, DiagnosisState.WAITING)


class EligibleWorkerTests(SimpleTestCase):
    def test_next_in_line(self):
        test = workload()
        found = diagnose(test, fleet([machine(seen=4)], [test]), Activity(), NOW)
        self.assertEqual(found.state, DiagnosisState.WAITING)
        self.assertEqual(found.severity, Severity.INFO)
        self.assertIn('was seen 4m ago and this workload is next in line', found.headline)

    def test_workers_that_took_it_failed(self):
        test = workload()
        errors = (error(8, '[Avalanche] dev build failed', 2, log_file='event8.log'), error(7, 'Engine crashed', 5))
        found = diagnose(test, fleet([machine(seen=1)], [test]), Activity(errors=errors), NOW)
        self.assertEqual(found.state, DiagnosisState.FAILING)
        self.assertIn('the last 2 workers to take it reported an error', found.headline)
        self.assertIn('"[Avalanche] dev build failed" 2m ago', found.headline)
        self.assertEqual(
            [item.link for item in found.evidence[:2]], [Link('/event/8/', 'Log 8'), Link('/errors/', 'Errors')]
        )

    def test_errors_before_the_last_result_are_resolved(self):
        test = workload()
        activity = Activity(last_result_at=minutes_ago(30), errors=(error(7, 'Engine crashed', 45),))
        found = diagnose(test, fleet([machine(seen=1)], [test]), activity, NOW)
        self.assertEqual(found.state, DiagnosisState.WAITING)

    def test_idle_fleet_still_shows_the_failures(self):
        test = workload()
        activity = Activity(errors=(error(7, 'Engine crashed', 45),))
        found = diagnose(test, fleet([machine(seen=40)], [test]), activity, NOW)
        self.assertEqual(found.state, DiagnosisState.NO_WORKERS)
        self.assertIn(EvidenceKind.ERROR, kinds(found))


class HeldWorkloadTests(SimpleTestCase):
    def test_running_normally(self):
        test = workload()
        workers = [machine(1, holds=10, concurrency=8), machine(2, holds=10, concurrency=8)]
        found = diagnose(test, fleet(workers, [test]), Activity(last_result_at=minutes_ago(1)), NOW)
        self.assertEqual(found.state, DiagnosisState.RUNNING)
        self.assertEqual(found.severity, Severity.OK)
        self.assertEqual(
            found.headline, 'Running normally: 2 workers (16 threads) on it. The last result arrived 1m ago.'
        )

    def test_running_before_the_first_result(self):
        test = workload()
        activity = Activity(last_assigned_at=minutes_ago(3))
        found = diagnose(test, fleet([machine(holds=10)], [test]), activity, NOW)
        self.assertEqual(found.state, DiagnosisState.RUNNING)
        self.assertIn('No result has been reported yet', found.headline)

    def test_stalled_when_holders_stop_reporting(self):
        test = workload()
        found = diagnose(test, fleet([machine(holds=10)], [test]), Activity(last_result_at=minutes_ago(25)), NOW)
        self.assertEqual(found.state, DiagnosisState.STALLED)
        self.assertEqual(found.severity, Severity.WARNING)
        self.assertIn('nothing has been reported for 25m (a result is expected within 10m)', found.headline)

    def test_long_time_controls_wait_longer(self):
        test = workload(dev_time_control='120.0+1.20')
        activity = Activity(last_result_at=minutes_ago(25))
        self.assertEqual(
            diagnose(test, fleet([machine(holds=10)], [test]), activity, NOW).state, DiagnosisState.RUNNING
        )

    def test_bulk_tunes_report_only_at_the_end(self):
        tune = workload(test_mode='SPSA')
        tune.spsa_run = SPSARun(reporting_type='BULK')
        activity = Activity(last_assigned_at=minutes_ago(50))
        self.assertEqual(
            diagnose(tune, fleet([machine(holds=10)], [tune]), activity, NOW).state, DiagnosisState.RUNNING
        )

    def test_a_holder_gone_silent_is_not_a_worker(self):
        test = workload()
        found = diagnose(test, fleet([machine(seen=5, holds=10)], [test]), Activity(), NOW)
        self.assertEqual(found.state, DiagnosisState.WAITING)

    def test_starting_while_workers_build(self):
        test = workload()
        activity = Activity(preparing=(Preparing(1, minutes_ago(4)),))
        found = diagnose(test, fleet([machine(seen=4, holds=10)], [test]), activity, NOW)
        self.assertEqual(found.state, DiagnosisState.STARTING)
        self.assertIn('handed to 1 worker, most recently 4m ago', found.headline)
        self.assertEqual(found.evidence[0].link, Link('/machines/1/', 'Machine 1'))

    def test_a_worker_that_failed_is_not_starting(self):
        test = workload()
        activity = Activity(preparing=(Preparing(1, minutes_ago(4)),), errors=(error(1, 'x build failed', 3),))
        found = diagnose(test, fleet([machine(seen=4, holds=10)], [test], frozenset({(10, 1)})), activity, NOW)
        self.assertEqual(found.state, DiagnosisState.NO_ELIGIBLE_WORKERS)
