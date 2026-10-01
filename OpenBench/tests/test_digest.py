from datetime import UTC, datetime, timedelta
from typing import Any
from unittest import mock

from django.core.cache import cache
from django.db import connection
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext

from OpenBench.config import OPENBENCH_CONFIG
from OpenBench.diagnosis.domain import DiagnosisState
from OpenBench.digest.domain import (
    DEFAULT_CHOICE,
    MAX_WINDOW,
    DigestReport,
    DigestWindow,
    GamesBucket,
    Preset,
    WindowChoice,
)
from OpenBench.digest.fleet import (
    bucket_hours,
    bucket_starts,
    classed_runs,
    core_hours,
    fleet_activity,
    games_by_hour,
    peak,
    pool_activity,
)
from OpenBench.digest.report import digest_report
from OpenBench.digest.serialize import digest_json
from OpenBench.digest.window import BadWindow, parse_choice, resolve
from OpenBench.insights.domain import Outcomes, WorkloadStatus
from OpenBench.models import Engine, Machine, Result, Test, WorkloadSnapshot
from OpenBench.progress.domain import Commit, HostCounters, RunMode, RunRow, RunStatus, TimeClass
from OpenBench.tests.fixtures import PASSWORD, create_engine_config, create_test, create_user, ensure_book, system_info
from OpenBench.triage.demo import record_error

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
PENTA = (5, 40, 100, 45, 10)
STRONG = (2, 20, 100, 60, 18)
LTC = '40.0+0.4'
DAY = Preset.HOURS_24


def window(preset: Preset = DAY, now: datetime = NOW) -> DigestWindow:
    return resolve(WindowChoice(preset, None), now)


def since(moment: datetime, now: datetime = NOW) -> DigestWindow:
    return resolve(WindowChoice(None, moment), now)


class WindowTests(SimpleTestCase):
    def test_nothing_chosen_is_the_last_24_hours(self) -> None:
        self.assertEqual(parse_choice(None, None), DEFAULT_CHOICE)
        self.assertEqual(parse_choice('', ''), DEFAULT_CHOICE)
        found = resolve(DEFAULT_CHOICE, NOW)
        self.assertEqual((found.since, found.until, found.clamped), (NOW - timedelta(hours=24), NOW, False))

    def test_presets_ignore_case_and_space(self) -> None:
        spans = {'8h': 8, ' 24H ': 24, '3d': 72, '7D': 168}
        for raw, hours in spans.items():
            with self.subTest(raw=raw):
                self.assertEqual(resolve(parse_choice(raw, None), NOW).span, timedelta(hours=hours))

    def test_unknown_presets_are_refused(self) -> None:
        for raw in ('12h', '30d', 'all', '24', '2026-10-01T00:00:00Z'):
            with self.subTest(raw=raw), self.assertRaises(BadWindow):
                parse_choice(raw, None)

    def test_a_timestamp_must_be_complete_and_carry_an_offset(self) -> None:
        for raw in (
            '2026-10-01',
            '2026-10-01T06:00',
            '2026-10-01T06:00:00',
            '2026-10-01 06:00:00Z',
            '2026-13-01T06:00:00Z',
            '2026-10-01T06:00:00Z ',
            '20261001T060000Z',
            'yesterday',
            '1790000000',
        ):
            with self.subTest(raw=raw), self.assertRaises(BadWindow):
                parse_choice(None, raw)

    def test_a_timestamp_starts_the_window_there(self) -> None:
        for raw in ('2026-10-01T06:00:00Z', '2026-10-01T06:00:00.000Z', '2026-10-01T08:00:00+02:00'):
            with self.subTest(raw=raw):
                found = resolve(parse_choice(None, raw), NOW)
                self.assertEqual((found.preset, found.since, found.clamped), (None, NOW - timedelta(hours=6), False))

    def test_a_timestamp_and_a_preset_together_are_refused(self) -> None:
        with self.assertRaises(BadWindow):
            parse_choice('8h', '2026-10-01T06:00:00Z')

    def test_the_future_is_refused(self) -> None:
        with self.assertRaises(BadWindow):
            since(NOW + timedelta(seconds=1))
        self.assertEqual(since(NOW).span, timedelta(0))

    def test_an_old_timestamp_is_clamped_to_thirty_days(self) -> None:
        exact = since(NOW - MAX_WINDOW)
        self.assertEqual((exact.since, exact.clamped), (NOW - MAX_WINDOW, False))
        older = since(NOW - MAX_WINDOW - timedelta(seconds=1))
        self.assertEqual((older.since, older.clamped), (NOW - MAX_WINDOW, True))

    def test_both_ends_belong_to_the_window(self) -> None:
        found = window()
        self.assertTrue(found.holds(found.since))
        self.assertTrue(found.holds(found.until))
        self.assertFalse(found.holds(found.since - timedelta(microseconds=1)))
        self.assertFalse(found.holds(found.until + timedelta(microseconds=1)))
        self.assertFalse(found.holds(None))


def run_row(run_id: int, time_class: TimeClass, games: int, hosts: tuple[HostCounters, ...] = ()) -> RunRow:
    return RunRow(
        id=run_id,
        engine='Avalanche',
        repo='',
        base=Commit('b'),
        dev=Commit('a'),
        subject='',
        author='author',
        mode=RunMode.SPRT,
        status=RunStatus.RUNNING,
        time_class=time_class,
        time_control='8.0+0.08',
        created_at=NOW,
        finished_at=None,
        games=games,
        outcomes=Outcomes((0, games, 0), (0, 0, games // 2, 0, 0), True),
        hosts=hosts,
    )


def counters(games: int, hours: float) -> tuple[HostCounters, ...]:
    side_ms = int(hours * 3_600_000 / 2)
    return (HostCounters('host', games, 10**9, side_ms, 10**9, side_ms),)


class FleetTests(SimpleTestCase):
    def hour(self, hours_ago: int) -> datetime:
        return NOW - timedelta(hours=hours_ago)

    def test_buckets_widen_with_the_window(self) -> None:
        spans = {8: 1, 24: 1, 72: 1, 73: 2, 168: 3, 720: 12}
        for hours, size in spans.items():
            with self.subTest(hours=hours):
                self.assertEqual(bucket_hours(timedelta(hours=hours)), size)

    def test_bucket_starts_cover_the_window_on_utc_boundaries(self) -> None:
        found = resolve(WindowChoice(Preset.DAYS_7, None), NOW + timedelta(minutes=20))
        starts = bucket_starts(found, 3)
        self.assertEqual(starts[0], NOW - timedelta(days=7))
        self.assertEqual(starts[-1], NOW)
        self.assertEqual(len(starts), 57)

    def test_hourly_games_are_differences_from_the_baseline(self) -> None:
        maxima = [(1, self.hour(2), 500), (1, self.hour(3), 300), (2, self.hour(2), 40), (1, self.hour(1), 480)]
        played = games_by_hour(maxima, {1: 250})
        self.assertEqual(
            played,
            {(1, self.hour(3)): 50, (1, self.hour(2)): 200, (1, self.hour(1)): 0, (2, self.hour(2)): 40},
        )

    def test_games_split_by_time_class_and_unknown_workloads_are_other(self) -> None:
        maxima = [(1, self.hour(2), 100), (2, self.hour(2), 60), (2, self.hour(1), 90), (3, self.hour(1), 8)]
        runs = [run_row(1, TimeClass.STC, 100), run_row(2, TimeClass.LTC, 90)]
        fleet = fleet_activity(window(Preset.HOURS_8), maxima, {}, runs, [])
        self.assertEqual(fleet.classes, [TimeClass.STC, TimeClass.LTC, TimeClass.OTHER])
        self.assertEqual(len(fleet.buckets), 9)
        self.assertEqual(fleet.buckets[-3], GamesBucket(self.hour(2), [100, 60, 0]))
        self.assertEqual(fleet.buckets[-2], GamesBucket(self.hour(1), [0, 30, 8]))
        self.assertEqual(fleet.games, 198)
        self.assertEqual((fleet.peak_games_per_hour, fleet.peak_at), (160.0, self.hour(2)))

    def test_core_hours_share_a_run_by_its_games_in_the_window(self) -> None:
        full = run_row(1, TimeClass.STC, 1000, counters(1000, 10.0))
        partial = run_row(2, TimeClass.STC, 1000, counters(500, 4.0))
        bare = run_row(3, TimeClass.STC, 1000)
        unrated = run_row(4, TimeClass.LTC, 1000)
        runs = classed_runs([full, partial, bare, unrated])

        measured = core_hours({1: 250}, runs)
        self.assertEqual((measured.hours, measured.estimated, measured.games_without), (2.5, False, 0))

        scaled = core_hours({2: 250}, runs)
        self.assertEqual((scaled.hours, scaled.estimated), (2.0, True))

        by_rate = core_hours({3: 150}, runs)
        self.assertAlmostEqual(by_rate.hours or 0.0, 150 * 14.0 / 1500)
        self.assertTrue(by_rate.estimated)

        unknown = core_hours({4: 100, 9: 20}, runs)
        self.assertEqual((unknown.hours, unknown.games_without), (None, 120))

        self.assertEqual(core_hours({}, runs).hours, 0.0)

    def test_hosts_are_pooled_by_name_pattern_and_cpu(self) -> None:
        hosts: list[tuple[str, str, object, object]] = [
            ('lab', 'k1', 'batch-a0741747-7d81-49a2-b96e-4b14d4c304c3:0', 'AMD EPYC 9R14'),
            ('lab', 'k2', 'batch-b0741747-7d81-49a2-b96e-4b14d4c304c3:1', 'AMD EPYC 9R14'),
            ('admin', 'k3', None, 'Apple M4'),
            ('admin', 'k3', None, 'Apple M4'),
        ]
        total, pools = pool_activity(hosts)
        self.assertEqual(total, 3)
        self.assertEqual(
            [(pool.label, pool.owner, pool.hosts) for pool in pools], [('batch-*', 'lab', 2), ('Apple M4', 'admin', 1)]
        )

    def test_a_quiet_window_has_no_peak(self) -> None:
        self.assertEqual(peak([GamesBucket(NOW, [0, 0])], 1), (None, None))
        self.assertEqual(peak([], 1), (None, None))


class DigestDataTests(TestCase):
    def setUp(self) -> None:
        create_engine_config()
        ensure_book()
        self.author = create_user('author')
        self.worker = create_user('worker')
        self.machine = Machine.objects.create(user=self.worker, info=system_info(machine_name='box', cpu_name='CPU'))
        cache.clear()

    def workload(self, penta: tuple[int, int, int, int, int] = PENTA, **fields: Any) -> Test:
        wins, losses = 2 * penta[4] + penta[3], 2 * penta[0] + penta[1]
        counts = {
            'games': 2 * sum(penta),
            'wins': wins,
            'losses': losses,
            'draws': 2 * sum(penta) - wins - losses,
            'LL': penta[0],
            'LD': penta[1],
            'DD': penta[2],
            'DW': penta[3],
            'WW': penta[4],
        }
        return create_test(self.author, **{**counts, **fields})

    def finished(self, at: datetime, took: timedelta = timedelta(hours=2), **fields: Any) -> Test:
        test = self.workload(**{'finished': True, 'passed': True, **fields})
        Test.objects.filter(id=test.id).update(creation=at - took, updated=at)
        WorkloadSnapshot.objects.create(test=test, created=at - took, games=0)
        WorkloadSnapshot.objects.create(test=test, created=at, games=test.games)
        return test

    def running(self, started: datetime, **fields: Any) -> Test:
        test = self.workload(**fields)
        Test.objects.filter(id=test.id).update(creation=started - timedelta(minutes=5), updated=NOW)
        WorkloadSnapshot.objects.create(test=test, created=started, games=0)
        WorkloadSnapshot.objects.create(test=test, created=NOW - timedelta(minutes=30), games=test.games)
        return test

    def pin(self, test: Test, base: str, dev: str) -> Test:
        Engine.objects.filter(id=test.dev_id).update(sha=(dev * 40)[:40])
        Engine.objects.filter(id=test.base_id).update(sha=(base * 40)[:40])
        return test

    def report(self, preset: Preset = DAY) -> DigestReport:
        return digest_report(window(preset))

    def finished_ids(self, report: DigestReport) -> list[int]:
        return [row.workload.id for row in report.finished.workloads]

    def test_a_quiet_server_has_an_empty_digest(self) -> None:
        report = self.report(Preset.HOURS_8)
        self.assertEqual(
            report.headline,
            ['Nothing finished in the last 8 hours.', 'The fleet played no games in the last 8 hours.'],
        )
        self.assertEqual((report.finished.counts.total, report.running.total, report.trunk), (0, 0, []))
        self.assertEqual((report.fleet.games, report.fleet.core_hours, report.fleet.hosts), (0, 0.0, 0))
        self.assertEqual(len(report.fleet.buckets), 9)
        self.assertEqual(report.errors.total, 0)

    def test_a_workload_that_finished_exactly_at_the_boundary_is_in(self) -> None:
        start = NOW - timedelta(hours=24)
        on_boundary = self.finished(start)
        self.finished(start - timedelta(seconds=1))
        inside = self.finished(NOW - timedelta(hours=1), failed=True, passed=False)
        at_the_end = self.finished(NOW)

        report = self.report()

        self.assertEqual(self.finished_ids(report), [at_the_end.id, inside.id, on_boundary.id])
        counts = report.finished.counts
        self.assertEqual((counts.total, counts.passed, counts.failed, counts.undecided), (3, 2, 1, 0))
        self.assertEqual(self.finished_ids(self.report(Preset.HOURS_8)), [at_the_end.id, inside.id])

    def test_the_finish_time_is_the_last_report_not_a_later_edit(self) -> None:
        old = self.finished(NOW - timedelta(days=3))
        Test.objects.filter(id=old.id).update(updated=NOW - timedelta(minutes=5))
        self.assertEqual(self.finished_ids(self.report()), [])

    def test_deleted_workloads_are_left_out(self) -> None:
        self.finished(NOW - timedelta(hours=2), deleted=True)
        self.running(NOW - timedelta(hours=2), deleted=True)
        report = self.report()
        self.assertEqual((report.finished.counts.total, report.finished.workloads, report.running.total), (0, [], 0))
        self.assertEqual(report.trunk, [])

    def test_finished_rows_carry_verdict_elo_duration_and_class(self) -> None:
        passed = self.finished(NOW - timedelta(hours=3), took=timedelta(hours=5), penta=STRONG)
        stopped = self.finished(
            NOW - timedelta(hours=4), passed=False, dev_time_control=LTC, base_time_control=LTC, info='Subject\ntag'
        )
        tune = self.finished(NOW - timedelta(hours=5), passed=False, test_mode='SPSA')

        rows = {row.workload.id: row for row in self.report().finished.workloads}

        first = rows[passed.id]
        self.assertEqual((first.status, first.games, first.duration_seconds), (WorkloadStatus.PASSED, 400, 18000.0))
        self.assertEqual(first.workload.time_class, TimeClass.STC)
        self.assertEqual(first.workload.url, f'/test/{passed.id}/')
        self.assertGreater(first.elo.value if first.elo else 0.0, 0.0)
        self.assertIsNone(first.stop_reason)

        second = rows[stopped.id]
        self.assertEqual((second.status, second.workload.time_class), (WorkloadStatus.STOPPED, TimeClass.LTC))

        third = rows[tune.id]
        self.assertEqual(
            (third.workload.url, third.elo, third.workload.time_class), (f'/tune/{tune.id}/', None, TimeClass.STC)
        )

    def test_a_running_workload_that_started_before_the_window_is_still_listed(self) -> None:
        old = self.running(NOW - timedelta(days=2))
        new = self.running(NOW - timedelta(hours=2), currentllr=1.5)
        pending = self.workload(approved=False)
        Test.objects.filter(id=pending.id).update(creation=NOW - timedelta(days=5))

        report = self.report()
        rows = {row.workload.id: row for row in report.running.workloads}

        self.assertEqual((report.running.total, report.running.pending, report.running.started), (3, 1, 1))
        self.assertEqual([row.workload.id for row in report.running.workloads], [new.id, old.id, pending.id])
        self.assertFalse(rows[old.id].started_in_window)
        self.assertTrue(rows[new.id].started_in_window)
        self.assertEqual(rows[old.id].started_at, NOW - timedelta(days=2))
        llr = rows[new.id].llr
        self.assertEqual((llr.value, llr.lower, llr.upper) if llr else None, (1.5, -2.94, 2.94))
        self.assertIsNotNone(rows[new.id].games_per_hour)
        self.assertIsNotNone(rows[new.id].eta)
        self.assertEqual(rows[pending.id].diagnosis.state, DiagnosisState.AWAITING_APPROVAL)
        self.assertEqual((rows[pending.id].eta, rows[pending.id].games_per_hour), (None, None))
        self.assertIn('2 still running, 1 awaiting approval', report.headline[0])

    def chain(self) -> dict[str, Test]:
        return {
            'old stc': self.pin(self.finished(NOW - timedelta(days=5)), 'r', 'a'),
            'old ltc': self.pin(
                self.finished(NOW - timedelta(days=4), dev_time_control=LTC, base_time_control=LTC), 'r', 'a'
            ),
            'new stc': self.pin(self.finished(NOW - timedelta(hours=20), penta=STRONG), 'a', 'b'),
            'new ltc': self.pin(
                self.finished(NOW - timedelta(hours=2), dev_time_control=LTC, base_time_control=LTC), 'a', 'b'
            ),
            'head stc': self.pin(self.finished(NOW - timedelta(hours=6)), 'b', 'c'),
            'head ltc': self.pin(
                self.running(NOW - timedelta(hours=1), dev_time_control=LTC, base_time_control=LTC), 'b', 'c'
            ),
        }

    def test_trunk_movement_is_split_by_time_class(self) -> None:
        tests = self.chain()

        (movement,) = self.report().trunk
        stc, ltc = movement.classes

        self.assertEqual((movement.engine, movement.trunk_length), ('Avalanche', 3))
        self.assertEqual((stc.time_class, ltc.time_class), (TimeClass.STC, TimeClass.LTC))
        self.assertEqual([move.index for move in stc.moves], [2, 3])
        self.assertEqual([move.runs for move in stc.moves], [[tests['new stc'].id], [tests['head stc'].id]])
        self.assertEqual((stc.measured, stc.accepted, stc.provisional), (2, 2, 0))
        steps = [move.elo.value for move in stc.moves if move.elo]
        self.assertAlmostEqual(stc.net.value if stc.net else 0.0, sum(steps))

        self.assertEqual([(move.index, move.provisional) for move in ltc.moves], [(2, False), (3, True)])
        self.assertEqual((ltc.measured, ltc.accepted, ltc.provisional), (1, 1, 1))
        self.assertEqual(ltc.net.value if ltc.net else None, ltc.moves[0].elo.value if ltc.moves[0].elo else None)
        self.assertIsNone(ltc.moves[1].measured_at)

    def test_a_narrower_window_keeps_only_what_finished_in_it(self) -> None:
        self.chain()
        (movement,) = self.report(Preset.HOURS_8).trunk
        stc, ltc = movement.classes
        self.assertEqual([move.index for move in stc.moves], [3])
        self.assertEqual((ltc.measured, ltc.provisional), (1, 1))
        headline = self.report(Preset.HOURS_8).headline[0]
        self.assertIn('Elo chained on the STC trunk', headline)
        self.assertIn('Elo chained on the LTC trunk', headline)

    def test_fleet_counts_games_hosts_and_search_time(self) -> None:
        test = self.running(NOW - timedelta(hours=30), games=1000)
        WorkloadSnapshot.objects.filter(test=test).delete()
        for hours_ago, games in ((30, 0), (26, 400), (3, 700), (2, 1000)):
            WorkloadSnapshot.objects.create(test=test, created=NOW - timedelta(hours=hours_ago), games=games)
        result = Result.objects.create(
            test=test,
            machine=self.machine,
            games=1000,
            dev_nodes=10**9,
            dev_time=18_000_000,
            base_nodes=10**9,
            base_time=18_000_000,
        )
        stale = Result.objects.create(test=test, machine=Machine.objects.create(user=self.worker, info={}), games=5)
        Result.objects.filter(id=result.id).update(updated=NOW - timedelta(hours=2))
        Result.objects.filter(id=stale.id).update(updated=NOW - timedelta(days=3))

        fleet = self.report().fleet

        self.assertEqual((fleet.games, fleet.classes), (600, [TimeClass.STC]))
        self.assertEqual((fleet.core_hours, fleet.core_hours_estimated, fleet.games_without_hours), (6.0, False, 0))
        self.assertEqual(fleet.hosts, 1)
        self.assertEqual([(pool.label, pool.cpu_name, pool.hosts) for pool in fleet.pools], [('box', 'CPU', 1)])
        self.assertEqual([bucket.total for bucket in fleet.buckets[-4:]], [300, 300, 0, 0])
        self.assertEqual(fleet.peak_games_per_hour, 300.0)

    def test_errors_seen_in_the_window_are_listed_unresolved_first(self) -> None:
        active = self.running(NOW - timedelta(days=3))
        done = self.finished(NOW - timedelta(days=2))
        record_error(active, self.machine.id, 'worker', 'Illegal Move', NOW - timedelta(days=3), None)
        record_error(active, self.machine.id, 'worker', 'Illegal Move', NOW - timedelta(hours=2), None)
        record_error(done, self.machine.id, 'worker', 'Disconnect', NOW - timedelta(hours=1), None)
        record_error(active, self.machine.id, 'worker', 'Time Loss', NOW - timedelta(days=2), None)

        errors = self.report().errors

        self.assertEqual((errors.total, errors.new, errors.unresolved), (2, 1, 1))
        self.assertEqual([line.row.group.signature.title for line in errors.lines], ['Illegal Move', 'Disconnect'])
        self.assertEqual([line.new for line in errors.lines], [False, True])
        self.assertEqual([line.row.verdict.resolved for line in errors.lines], [False, True])

    def test_the_report_serializes(self) -> None:
        self.chain()
        payload = digest_json(self.report())
        self.assertEqual(
            list(payload), ['generated_at', 'window', 'headline', 'finished', 'running', 'trunk', 'fleet', 'errors']
        )
        self.assertEqual(
            payload['window'],
            {
                'preset': '24h',
                'since': (NOW - timedelta(hours=24)).isoformat(),
                'until': NOW.isoformat(),
                'clamped': False,
            },
        )

    def test_query_count_does_not_grow_with_the_data(self) -> None:
        tests = self.chain()
        record_error(tests['head ltc'], self.machine.id, 'worker', 'Illegal Move', NOW - timedelta(hours=1), None)

        def queries() -> int:
            with CaptureQueriesContext(connection) as captured:
                self.report(Preset.DAYS_7)
            return len(captured)

        first = queries()
        for index in range(12):
            self.finished(NOW - timedelta(hours=index + 1))
            running = self.running(NOW - timedelta(hours=index + 1))
            record_error(running, self.machine.id, 'worker', f'[x] failure {index}', NOW - timedelta(hours=1), None)
        self.assertEqual(queries(), first)


class DigestViewTests(TestCase):
    def setUp(self) -> None:
        create_engine_config()
        ensure_book()
        self.reader = create_user('reader')
        self.test = create_test(self.reader, finished=True, passed=True, games=400, LL=5, LD=40, DD=100, DW=45, WW=10)
        WorkloadSnapshot.objects.create(test=self.test, games=400)
        cache.clear()

    def login(self) -> None:
        self.client.force_login(self.reader)

    def test_anonymous_is_redirected(self) -> None:
        response = self.client.get('/digest/?since=8h')
        self.assertEqual((response.status_code, response['Location']), (302, '/login/'))

    def test_a_public_server_shows_the_page(self) -> None:
        with mock.patch.dict(OPENBENCH_CONFIG, {'require_login_to_view': False}):
            self.assertContains(self.client.get('/digest/'), '1 workload finished in the last 24 hours')

    def test_page_lists_the_window_and_links_the_row(self) -> None:
        self.login()
        response = self.client.get('/digest/?since=8h')
        self.assertContains(response, '1 workload finished in the last 8 hours (1 passed)')
        self.assertContains(response, f'<tr data-row-href="/test/{self.test.id}/">')
        self.assertContains(response, '<a href="/digest/?since=8h" class="fleet-filter-option" aria-current="true">')
        self.assertContains(response, 'id="digest-data" type="application/json"')
        self.assertContains(response, '<a href="/digest/"><i class="fa-solid fa-newspaper fa-fw"')

    def test_a_quiet_window_says_so(self) -> None:
        Test.objects.all().update(deleted=True)
        WorkloadSnapshot.objects.all().delete()
        self.login()
        response = self.client.get('/digest/?since=8h')
        for text in (
            'Nothing finished in the last 8 hours.',
            'Nothing is running or waiting for approval.',
            'No trunk step was measured in the last 8 hours.',
            'No games were recorded in the last 8 hours.',
            'No worker error was reported in the last 8 hours.',
        ):
            self.assertContains(response, text)
        self.assertNotContains(response, 'data-digest-chart')

    def test_a_timestamp_window_is_marked_current(self) -> None:
        self.login()
        response = self.client.get('/digest/', {'from': '2026-01-01T00:00:00Z'})
        page = response.context['page']
        self.assertTrue(page.window.clamped)
        self.assertEqual([option.current for option in page.options], [False, False, False, False, True])
        self.assertContains(response, 'a digest reaches back 30 days at most')

    def test_a_bad_window_falls_back_to_the_default(self) -> None:
        self.login()
        for query in (
            'since=decade',
            'from=yesterday',
            'from=2999-01-01T00:00:00Z',
            'since=8h&from=2026-01-01T00:00:00Z',
        ):
            with self.subTest(query=query):
                self.assertEqual(self.client.get(f'/digest/?{query}').context['page'].window.preset, DAY)

    def test_fixed_windows_are_cached_briefly_and_timestamps_never(self) -> None:
        self.login()
        self.client.get('/digest/')
        Test.objects.filter(id=self.test.id).update(deleted=True)
        self.assertEqual(len(self.client.get('/digest/').context['page'].finished), 1)
        recent = self.client.get('/digest/', {'from': '2026-01-01T00:00:00Z'})
        self.assertEqual(len(recent.context['page'].finished), 0)
        cache.clear()
        self.assertEqual(len(self.client.get('/digest/').context['page'].finished), 0)

    def test_api_requires_authentication(self) -> None:
        response = self.client.get('/api/digest/')
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json(), {'error': 'API requires authentication for this server'})

    def test_api_accepts_posted_credentials_and_window(self) -> None:
        response = self.client.post('/api/digest/', {'username': 'reader', 'password': PASSWORD, 'since': '7d'})
        self.assertEqual(response.status_code, 200)
        digest = response.json()['digest']
        self.assertEqual(digest['window']['preset'], '7d')
        self.assertEqual(digest['finished']['counts'], {'total': 1, 'passed': 1, 'failed': 0})
        self.assertEqual(digest['finished']['workloads'][0]['workload']['id'], self.test.id)
        self.assertEqual(digest['errors']['groups'], [])

    def test_api_refuses_a_bad_window(self) -> None:
        self.login()
        for query in (
            'since=decade',
            'from=yesterday',
            'from=2999-01-01T00:00:00Z',
            'since=8h&from=2026-01-01T00:00:00Z',
        ):
            with self.subTest(query=query):
                response = self.client.get(f'/api/digest/?{query}')
                self.assertEqual(response.status_code, 400)
                self.assertIn('error', response.json())
