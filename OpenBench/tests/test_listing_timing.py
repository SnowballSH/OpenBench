import random
import re
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from OpenBench.insights.domain import Outcomes, SprtBounds, WorkloadFacts, WorkloadMode, WorkloadStatus
from OpenBench.insights.eta import EtaReason, timing_and_eta
from OpenBench.insights.listing import (
    ESTIMATE_NOTE,
    ETA_REASON_TEXT,
    RowTiming,
    RowTimingKind,
    SnapshotMarks,
    finished_row_timing,
    format_duration,
    format_rate,
    running_row_timing,
)
from OpenBench.insights.sources import workload_facts
from OpenBench.insights.timing import RECENT_WINDOW, Mark, timeline_marks
from OpenBench.insights.workload import workload_insights
from OpenBench.models import Test, WorkloadSnapshot
from OpenBench.page_queries import annotated_snapshots, listing_row_timing, listing_tests
from OpenBench.tests.fixtures import create_engine_config, create_test, create_user, ensure_book

T0 = datetime(2026, 9, 1, tzinfo=UTC)
BOUNDS = SprtBounds(elo0=0.0, elo1=3.0, lower_llr=-2.94, upper_llr=2.94)
PENTA = (39, 884, 2667, 924, 44)
INSIGHTS_JS = Path(__file__).resolve().parents[1] / 'static' / 'insights.js'


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def outcomes(penta: Sequence[int] = PENTA) -> Outcomes:
    wins = 2 * penta[4] + penta[3]
    losses = 2 * penta[0] + penta[1]
    return Outcomes((losses, 2 * sum(penta) - wins - losses, wins), tuple(penta), True)


def facts(
    mode: WorkloadMode = WorkloadMode.GAMES,
    penta: Sequence[int] = PENTA,
    target: int | None = 20_000,
    llr: float = 0.5,
    updated: datetime = T0,
) -> WorkloadFacts:
    return WorkloadFacts(
        id=1,
        mode=mode,
        status=WorkloadStatus.ACTIVE,
        created_at=T0,
        updated_at=updated,
        finished=False,
        outcomes=outcomes(penta),
        llr=llr,
        sprt=BOUNDS if mode == WorkloadMode.SPRT else None,
        target_games=target,
    )


def picked(snapshots: Sequence[Mark], now: datetime) -> SnapshotMarks:
    window_start = now - RECENT_WINDOW
    before = [mark for mark in snapshots if mark[0] <= window_start]
    after = [mark for mark in snapshots if mark[0] > window_start]
    return SnapshotMarks(
        first=snapshots[0] if snapshots else None,
        window_before=before[-1] if before else None,
        window_after=after[0] if after else None,
        latest=snapshots[-1] if snapshots else None,
    )


def history(rng: random.Random, points: int, span_minutes: float) -> list[Mark]:
    times = sorted(rng.uniform(0, span_minutes) for _ in range(points))
    games, marks = 0, []
    for minutes in times:
        games += rng.choice((0, 8, 16, 64))
        marks.append((at(minutes), games))
    return marks


class FormatTests(SimpleTestCase):
    def test_durations_match_the_insights_tiles(self) -> None:
        cases = {
            0: '0s',
            59.4: '59s',
            60: '1m',
            3600: '1h',
            3600 + 20 * 60: '1h 20m',
            86400: '1d',
            5 * 86400 + 5 * 3600 + 59: '5d 5h',
            -30: '0s',
        }
        for seconds, text in cases.items():
            with self.subTest(seconds=seconds):
                self.assertEqual(format_duration(seconds), text)

    def test_rates_round_like_the_insights_tiles(self) -> None:
        self.assertEqual(format_rate(25.35), '25.4')
        self.assertEqual(format_rate(99.94), '99.9')
        self.assertEqual(format_rate(1234.5), '1,234')

    def test_reasons_use_the_workload_page_wording(self) -> None:
        source = INSIGHTS_JS.read_text()
        block = re.search(r'const ETA_REASONS = \{(.*?)\};', source, re.S).group(1)
        wording = dict(re.findall(r"(\w+): '([^']*)'", block))
        self.assertEqual(wording, {reason.value: text for reason, text in ETA_REASON_TEXT.items()})
        self.assertEqual(set(ETA_REASON_TEXT), set(EtaReason))


class SnapshotSubsetTests(SimpleTestCase):
    def assert_same_measurement(self, snapshots: list[Mark], current: Mark, now: datetime) -> None:
        workload = facts(updated=current[0], penta=(0, 0, current[1] // 2, 0, 0))
        full = timing_and_eta(workload, timeline_marks(T0, current, snapshots), now)
        subset = timing_and_eta(workload, timeline_marks(T0, current, picked(snapshots, now).marks()), now)
        self.assertEqual(subset, full)

    def test_the_picked_snapshots_measure_like_the_whole_history(self) -> None:
        rng = random.Random(20260930)
        for case in range(200):
            points = rng.choice((0, 1, 2, 5, 40, 300))
            span = rng.choice((3, 50, 90, 600, 3000))
            snapshots = history(rng, points, span)
            last_games = snapshots[-1][1] if snapshots else 0
            current = (at(span + rng.uniform(0, 5)), last_games + rng.choice((0, 0, 2, 2 * rng.randrange(1, 50))))
            now = at(span + rng.choice((0.5, 10, 45, 61, 300)))
            with self.subTest(case=case, points=points, span=span):
                self.assert_same_measurement(snapshots, current, now)

    def test_duplicate_picks_collapse(self) -> None:
        only = (at(0), 10)
        self.assertEqual(SnapshotMarks(only, only, None, only).marks(), [only])


class RowTimingTests(SimpleTestCase):
    steady = SnapshotMarks(
        first=(at(0), 0), window_before=(at(59), 1180), window_after=(at(61), 1220), latest=(at(120), 2400)
    )

    def test_target_workloads_show_time_left_and_rate(self) -> None:
        row = running_row_timing(facts(penta=(0, 0, 1200, 0, 0), updated=at(120)), self.steady, at(120))
        self.assertEqual(row, RowTiming(RowTimingKind.LEFT, '14h 40m left', False, '1,200 games/h', 'last 1h'))
        self.assertIsNone(row.note)

    def test_sprt_time_left_is_an_estimate(self) -> None:
        workload = facts(WorkloadMode.SPRT, target=None, llr=0.5, updated=at(120))
        row = running_row_timing(workload, self.steady, at(120))
        self.assertEqual(row.kind, RowTimingKind.LEFT)
        self.assertTrue(row.estimate)
        self.assertEqual(row.note, ESTIMATE_NOTE)
        self.assertRegex(row.text, r'^\d+[dhms]( \d+[hm])? left$')

    def test_sprt_reasons_replace_the_time(self) -> None:
        workload = facts(WorkloadMode.SPRT, penta=(10, 10, 10, 10, 10), target=None, llr=0.1, updated=at(120))
        row = running_row_timing(workload, self.steady, at(120))
        self.assertEqual(
            (row.kind, row.text, row.estimate), (RowTimingKind.UNAVAILABLE, 'needs 200 games first', False)
        )

    def test_a_stalled_workload_has_no_rate(self) -> None:
        stalled = SnapshotMarks(
            first=(at(0), 0), window_before=(at(60), 1200), window_after=None, latest=(at(60), 1200)
        )
        row = running_row_timing(facts(penta=(0, 0, 600, 0, 0), updated=at(60)), stalled, at(180))
        self.assertEqual(row, RowTiming(RowTimingKind.UNAVAILABLE, 'no recent throughput'))

    def test_no_target_shows_only_the_rate(self) -> None:
        workload = facts(WorkloadMode.SPSA, penta=(0, 0, 1200, 0, 0), target=None, updated=at(120))
        row = running_row_timing(workload, self.steady, at(120))
        self.assertEqual((row.kind, row.text, row.rate), (RowTimingKind.RATE_ONLY, None, '1,200 games/h'))

    def test_a_young_workload_measures_its_whole_life(self) -> None:
        young = SnapshotMarks(first=(at(0), 0), window_before=None, window_after=(at(0), 0), latest=(at(4), 40))
        row = running_row_timing(facts(penta=(0, 0, 50, 0, 0), updated=at(10)), young, at(10))
        self.assertEqual((row.rate, row.rate_window), ('600 games/h', 'last 10m'))

    def test_nothing_to_say(self) -> None:
        empty = SnapshotMarks(None, None, None, None)
        self.assertIsNone(running_row_timing(facts(WorkloadMode.SPSA, penta=(0,) * 5, target=None), empty, at(1)))

    def test_time_taken(self) -> None:
        self.assertEqual(finished_row_timing(at(0), at(312)), RowTiming(RowTimingKind.TOOK, 'took 5h 12m'))
        self.assertEqual(finished_row_timing(at(5), at(0)).text, 'took 0s')


class ListingTimingTests(TestCase):
    def setUp(self) -> None:
        create_engine_config()
        ensure_book()
        self.author = create_user('author')
        self.client.force_login(self.author)
        self.now = timezone.now()

    def workload(self, snapshots: Sequence[Mark], created: datetime, **fields: object) -> Test:
        test = create_test(self.author, **fields)
        WorkloadSnapshot.objects.bulk_create(
            WorkloadSnapshot(test=test, created=moment, games=games) for moment, games in snapshots
        )
        Test.objects.filter(id=test.id).update(creation=created, updated=fields.get('updated', self.now))
        return Test.objects.get(id=test.id)

    def minutes_ago(self, minutes: float) -> datetime:
        return self.now - timedelta(minutes=minutes)

    def steady(self, minutes: int, per_minute: int) -> list[Mark]:
        return [(self.minutes_ago(minutes - index), index * per_minute) for index in range(minutes + 1)]

    def listed(self, test: Test) -> Test:
        return listing_tests(Test.objects.filter(id=test.id), self.now).get()

    def test_annotations_measure_like_the_workload_page(self) -> None:
        history = self.steady(180, 16)
        penta = (40, 900, 2700, 900, 40)
        test = self.workload(
            history,
            self.minutes_ago(200),
            test_mode='SPRT',
            games=2 * sum(penta),
            LL=penta[0],
            LD=penta[1],
            DD=penta[2],
            DW=penta[3],
            WW=penta[4],
            wins=2 * penta[4] + penta[3],
            losses=2 * penta[0] + penta[1],
            draws=2 * sum(penta) - (2 * penta[4] + penta[3]) - (2 * penta[0] + penta[1]),
            currentllr=0.4,
        )
        listed = self.listed(test)
        insights = workload_insights(test, self.now)

        marks = timeline_marks(listed.creation, (listed.updated, listed.games), annotated_snapshots(listed).marks())
        timing, eta = timing_and_eta(workload_facts(listed), marks, self.now)
        self.assertEqual((timing, eta), (insights.timing, insights.eta))
        self.assertEqual(listing_row_timing(listed).text, f'{format_duration(insights.eta.remaining_seconds)} left')

    def test_finished_rows_took_from_first_snapshot_to_last_report(self) -> None:
        test = self.workload(
            [(self.minutes_ago(400), 0), (self.minutes_ago(88), 800)],
            self.minutes_ago(500),
            finished=True,
            passed=True,
            games=800,
        )
        self.assertEqual(listing_row_timing(self.listed(test)).text, 'took 5h 12m')

    def test_finished_rows_without_history_start_at_creation(self) -> None:
        test = self.workload([], self.minutes_ago(90), finished=True, games=100)
        self.assertEqual(listing_row_timing(self.listed(test)).text, 'took 1h 30m')

    def test_pending_rows_and_bare_tests_show_nothing(self) -> None:
        pending = self.workload(self.steady(10, 1), self.minutes_ago(10), approved=False)
        self.assertIsNone(listing_row_timing(self.listed(pending)))
        with self.assertNumQueries(0):
            self.assertIsNone(listing_row_timing(pending))

    def test_listings_show_timing(self) -> None:
        self.workload(
            self.steady(90, 10), self.minutes_ago(90), test_mode='GAMES', max_games=2000, games=900, draws=900
        )
        self.workload(
            [(self.minutes_ago(400), 0), (self.minutes_ago(88), 800)],
            self.minutes_ago(500),
            finished=True,
            passed=True,
            games=800,
            elolower=0.0,
            eloupper=3.0,
        )

        for url in ('/index/', '/user/author/'):
            with self.subTest(url=url):
                content = self.client.get(url).content.decode()
                self.assertIn('<span class="row-timing-left">1h 50m left</span>', content)
                self.assertIn('600 games/h', content)
                self.assertIn('<span class="row-timing-took">took 5h 12m</span>', content)

        for url in ('/greens/', '/search/?authors=author'):
            with self.subTest(url=url):
                self.assertIn('took 5h 12m', self.client.get(url).content.decode())
