import math
import random
from datetime import UTC, date, datetime, timedelta

from django.test import SimpleTestCase
from scipy.stats import t as student_t

from OpenBench.insights.domain import Outcomes
from OpenBench.progress.domain import (
    Commit,
    HostCounters,
    RunMode,
    RunRow,
    RunStatus,
    SpeedRatio,
    Step,
    TimeClass,
)
from OpenBench.progress.economics import economics
from OpenBench.progress.lineage import build_lineage, build_steps
from OpenBench.progress.present import (
    bench_text,
    cost_text,
    duration_text,
    economics_page,
    failed_tile,
    speed_text,
    velocity_tile,
)
from OpenBench.progress.speed import speed_of, speed_series
from OpenBench.tests.fixtures import present

START = datetime(2026, 9, 1, tzinfo=UTC)
PENTA = (5, 40, 100, 45, 10)
WEAK = (12, 50, 100, 35, 4)
STRONG = (2, 20, 100, 60, 18)
MS_PER_HOUR = 3_600_000


def host(name: str, ratio: float = 1.0, ms: int = MS_PER_HOUR, games: int = 400) -> HostCounters:
    return HostCounters(name, games, round(1_000_000 * ratio), ms, 1_000_000, ms)


def uncounted(name: str, games: int = 400) -> HostCounters:
    return HostCounters(name, 0, 0, 0, 0, 0)


class Rows:
    def __init__(self) -> None:
        self.rows: list[RunRow] = []

    def run(
        self,
        base: str,
        dev: str,
        hosts: tuple[HostCounters, ...] = (),
        status: RunStatus = RunStatus.PASSED,
        time_class: TimeClass = TimeClass.STC,
        penta: tuple[int, int, int, int, int] = PENTA,
        created: datetime | None = None,
        hours: float = 2.0,
        threads: int = 1,
        base_threads: int | None = None,
        mode: RunMode = RunMode.SPRT,
        bench: tuple[int, int] = (0, 0),
    ) -> RunRow:
        id = len(self.rows) + 1
        created = created or START + timedelta(days=id)
        finished = status not in (RunStatus.RUNNING, RunStatus.PENDING)
        wins, losses = 2 * penta[4] + penta[3], 2 * penta[0] + penta[1]
        row = RunRow(
            id=id,
            engine='Avalanche',
            repo='https://github.com/SnowballSH/Avalanche',
            base=Commit(base),
            dev=Commit(dev),
            subject=f'{base} to {dev}',
            author='author',
            mode=mode,
            status=status,
            time_class=time_class,
            time_control='8.0+0.08',
            created_at=created,
            finished_at=created + timedelta(hours=hours + 1) if finished else None,
            games=2 * sum(penta),
            outcomes=Outcomes((losses, 2 * sum(penta) - wins - losses, wins), penta, True),
            dev_threads=threads,
            base_threads=threads if base_threads is None else base_threads,
            started_at=created + timedelta(hours=1),
            base_bench=bench[0],
            dev_bench=bench[1],
            hosts=hosts,
        )
        self.rows.append(row)
        return row

    def steps(self) -> list[Step]:
        return build_steps(self.rows)

    def step(self) -> Step:
        (step,) = self.steps()
        return step


class SpeedRatioTests(SimpleTestCase):
    def test_a_single_host_gives_a_ratio_without_an_interval(self):
        speed = present(speed_of([host('a', 0.98)]))
        self.assertAlmostEqual(speed.ratio, 0.98)
        self.assertEqual((speed.lower, speed.upper, speed.hosts, speed.games), (None, None, 1, 400))

    def test_hosts_are_weighted_by_time_and_scatter_gives_the_interval(self):
        speed = present(speed_of([host('a', 0.96), host('b', 1.0)]))
        mean = (math.log(0.96) + math.log(1.0)) / 2
        margin = float(student_t.ppf(0.975, 1)) * abs(math.log(0.96)) / 2
        self.assertAlmostEqual(speed.ratio, math.exp(mean))
        self.assertAlmostEqual(present(speed.lower), math.exp(mean - margin))
        self.assertAlmostEqual(present(speed.upper), math.exp(mean + margin))

        heavy = present(speed_of([host('a', 0.96, ms=3 * MS_PER_HOUR), host('b', 1.0)]))
        self.assertAlmostEqual(heavy.ratio, math.exp(0.75 * math.log(0.96)))

    def test_the_ratio_is_nodes_per_second_not_nodes(self):
        slower_clock = HostCounters('a', 400, 1_000_000, 2000, 1_000_000, 1000)
        self.assertAlmostEqual(present(speed_of([slower_clock])).ratio, 0.5)

    def test_one_host_seen_in_several_runs_is_one_observation(self):
        speed = present(speed_of([host('a', 0.9), host('a', 1.0)]))
        self.assertEqual((speed.hosts, speed.lower, speed.games), (1, None, 800))
        self.assertAlmostEqual(speed.ratio, 0.95)

    def test_results_without_counters_or_with_zero_time_are_left_out(self):
        zero_time = HostCounters('z', 400, 1_000_000, 0, 1_000_000, 1000)
        self.assertIsNone(speed_of([uncounted('a'), zero_time]))
        self.assertIsNone(speed_of([]))
        self.assertEqual(present(speed_of([uncounted('a'), zero_time, host('b', 0.97)])).hosts, 1)

    def test_a_handful_of_games_is_no_measurement(self):
        self.assertIsNone(speed_of([host('a', 0.5, games=20)]))

    def test_identical_hosts_give_a_degenerate_interval(self):
        speed = present(speed_of([host('a', 0.97), host('b', 0.97)]))
        self.assertAlmostEqual(present(speed.lower), speed.ratio)


class StepSpeedTests(SimpleTestCase):
    def test_a_step_pools_every_run_and_lists_its_classes(self):
        rows = Rows()
        rows.run('a', 'b', (host('x', 0.98), host('y', 0.98)))
        rows.run('a', 'b', (host('x', 0.98), host('z', 0.98)), time_class=TimeClass.LTC)
        speed = present(rows.step().speed)
        self.assertEqual(speed.pooled.hosts, 3)
        self.assertEqual([found.time_class for found in speed.classes], [TimeClass.STC, TimeClass.LTC])
        self.assertFalse(speed.classes_differ)

    def test_classes_that_disagree_beyond_their_margins_are_flagged(self):
        rows = Rows()
        rows.run('a', 'b', (host('x', 0.980), host('y', 0.981)))
        rows.run('a', 'b', (host('x', 0.900), host('y', 0.901)), time_class=TimeClass.LTC)
        speed = present(rows.step().speed)
        self.assertTrue(speed.classes_differ)
        self.assertIn('; STC −2.0%, LTC −10.0%', speed_text(speed))

    def test_classes_without_intervals_are_never_called_different(self):
        rows = Rows()
        rows.run('a', 'b', (host('x', 0.98),))
        rows.run('a', 'b', (host('y', 0.90),), time_class=TimeClass.LTC)
        self.assertFalse(present(rows.step().speed).classes_differ)

    def test_unlike_conditions_are_left_out_of_speed(self):
        rows = Rows()
        rows.run('a', 'b', (host('x', 0.5),), time_class=TimeClass.OTHER)
        self.assertIsNone(rows.step().speed)

    def test_a_step_without_counters_has_no_speed(self):
        rows = Rows()
        rows.run('a', 'b', (uncounted('x'),))
        step = rows.step()
        self.assertIsNone(step.speed)
        self.assertEqual(speed_text(step.speed), '')

    def test_text(self):
        rows = Rows()
        rows.run('a', 'b', (host('x', 0.98),))
        self.assertEqual(speed_text(rows.step().speed), 'speed −2.0% (one host, no interval)')


class SpeedChainTests(SimpleTestCase):
    def chain(self, *hosts_per_step: tuple[HostCounters, ...]) -> list[Step]:
        rows = Rows()
        names = 'abcdefgh'
        for index, hosts in enumerate(hosts_per_step):
            rows.run(names[index], names[index + 1], hosts)
        return rows.steps()

    def test_ratios_multiply_and_log_margins_add_in_quadrature(self):
        steps = self.chain((host('x', 0.98), host('y', 0.99)), (host('x', 0.96), host('y', 0.97)))
        series = speed_series(steps, 1)
        first, second = (present(step.speed).pooled for step in steps)
        total = present(series.total)
        self.assertAlmostEqual(total.ratio, first.ratio * second.ratio)

        def margin(speed: SpeedRatio) -> float:
            return (math.log(present(speed.upper)) - math.log(present(speed.lower))) / 2

        chained = math.hypot(margin(first), margin(second))
        self.assertAlmostEqual(present(total.upper), total.ratio * math.exp(chained))
        self.assertAlmostEqual(present(total.lower), total.ratio / math.exp(chained))
        self.assertEqual((series.measured, series.unbounded, series.steps), (2, 0, 2))
        self.assertEqual([point.index for point in series.points], [1, 2])
        self.assertEqual(series.points[0].cumulative, present(series.points[0].cumulative))

    def test_a_single_host_step_takes_the_interval_away_from_then_on(self):
        steps = self.chain((host('x', 0.98), host('y', 0.99)), (host('x', 0.96),), (host('x', 0.99), host('y', 0.99)))
        series = speed_series(steps, 1)
        bounded = [present(point.cumulative).lower is not None for point in series.points]
        self.assertEqual(bounded, [True, False, False])
        self.assertIsNone(present(series.total).lower)
        self.assertEqual(series.unbounded, 1)
        self.assertAlmostEqual(present(series.total).ratio, math.exp(math.log(0.98 * 0.99) / 2) * 0.96 * 0.99)

    def test_a_step_without_counters_is_a_gap_that_adds_nothing(self):
        steps = self.chain((host('x', 0.98),), (uncounted('x'),), (host('x', 0.5, ms=0),), (host('x', 0.97),))
        series = speed_series(steps, 5)
        self.assertEqual([point.cumulative is None for point in series.points], [False, True, True, False])
        self.assertEqual([point.index for point in series.points], [5, 6, 7, 8])
        self.assertAlmostEqual(present(series.total).ratio, 0.98 * 0.97)
        self.assertEqual((series.measured, series.steps), (2, 4))

    def test_nothing_measured_has_no_total(self):
        series = speed_series(self.chain((), ()), 1)
        self.assertIsNone(series.total)
        self.assertEqual(series.measured, 0)


class StepCostTests(SimpleTestCase):
    def test_cost_adds_every_run(self):
        rows = Rows()
        rows.run('a', 'b', (host('x'), host('y', ms=MS_PER_HOUR // 2)), hours=3)
        rows.run('a', 'b', (host('x'),), time_class=TimeClass.SMP, threads=4, hours=5)
        rows.run('a', 'b', status=RunStatus.RUNNING, time_class=TimeClass.LTC)
        cost = rows.step().cost
        self.assertEqual((cost.runs, cost.games, cost.counted_games), (3, 1200, 1200))
        self.assertEqual(cost.decision_seconds, 8 * 3600)
        self.assertAlmostEqual(present(cost.core_hours), 2 + 1 + 4 * 2)

    def test_missing_counters_and_unfinished_runs_have_no_measure(self):
        rows = Rows()
        rows.run('a', 'b', (uncounted('x'),), status=RunStatus.RUNNING)
        cost = rows.step().cost
        self.assertEqual((cost.decision_seconds, cost.core_hours, cost.counted_games), (None, None, 0))
        self.assertEqual(cost_text(cost), '400 games')

    def test_run_carries_its_own_cost(self):
        rows = Rows()
        rows.run('a', 'b', (host('x'), uncounted('y')))
        (run,) = rows.step().measurements[0].runs
        self.assertEqual((run.counted_games, run.core_hours), (400, 2.0))

    def test_text(self):
        rows = Rows()
        rows.run('a', 'b', (host('x'),), hours=3)
        self.assertEqual(cost_text(rows.step().cost), '400 games · 3.0 h under test · 2.0 core-h')
        self.assertEqual(duration_text(None), '—')
        self.assertEqual(duration_text(45 * 60), '45 min')
        self.assertEqual(duration_text(3 * 86400), '3.0 d')


class BenchTests(SimpleTestCase):
    def test_bench_names_an_unchanged_value(self):
        rows = Rows()
        rows.run('a', 'b', bench=(100, 1234567))
        rows.run('b', 'c', bench=(1234567, 1234567))
        rows.run('c', 'd')
        changed, same, unknown = rows.steps()
        self.assertEqual((changed.base_bench, changed.dev_bench), (100, 1234567))
        self.assertEqual(bench_text(changed), 'bench 1,234,567')
        self.assertEqual(bench_text(same), 'bench 1,234,567, same as base')
        self.assertEqual((unknown.dev_bench, bench_text(unknown)), (None, ''))


class EconomicsTests(SimpleTestCase):
    def setUp(self):
        rows = Rows()
        day = timedelta(days=1)
        rows.run('a', 'b', (host('x', 0.98, games=200), host('y', 0.98, games=200)), created=START, hours=2)
        rows.run('a', 'b', (host('y', 0.98),), time_class=TimeClass.LTC, created=START + day, hours=4)
        rows.run('b', 'c', (host('x', 0.99),), created=START + 8 * day, hours=6)
        rows.run('b', 'f', (host('x'),), status=RunStatus.FAILED, penta=WEAK, created=START + 2 * day)
        rows.run('b', 'g', (uncounted('x'),), status=RunStatus.RUNNING, created=START + 9 * day)
        self.rows = rows
        self.lineage = present(build_lineage(rows.steps()))
        self.start, self.end = date(2026, 8, 25), date(2026, 9, 14)
        self.report = economics(self.lineage, None, self.start, self.end)

    def test_buckets_split_the_window_by_fate(self):
        report = self.report
        self.assertEqual((report.trunk.steps, report.failed.steps, report.other.steps), (2, 1, 1))
        self.assertEqual((report.trunk.games, report.failed.games, report.other.games), (1200, 402, 400))
        self.assertAlmostEqual(report.trunk.core_hours, 4 + 2 + 2)
        self.assertEqual(report.failed.core_hours, 2.0)
        self.assertAlmostEqual(present(report.failed.estimated_core_hours), 2 * 402 / 400)
        self.assertAlmostEqual(present(report.other.estimated_core_hours), 400 * 8 / 1200)
        total = 8 + 2 * 402 / 400 + 400 * 8 / 1200
        self.assertAlmostEqual(present(report.failed.core_share), (2 * 402 / 400) / total)
        self.assertTrue(report.core_hours_estimated)
        self.assertAlmostEqual(present(report.failed.games_share), 402 / 2002)
        self.assertAlmostEqual(present(report.counter_coverage), 1600 / 2002)
        shares = [present(found.games_share) for found in (report.trunk, report.failed, report.other)]
        self.assertAlmostEqual(sum(shares), 1.0)

    def test_classes_count_decided_sprt_runs(self):
        stc, ltc = self.report.classes
        self.assertEqual((stc.time_class, stc.passed, stc.failed, stc.pass_rate), (TimeClass.STC, 2, 1, 2 / 3))
        self.assertEqual((stc.median_games_to_pass, stc.median_games_to_fail), (400.0, 402.0))
        self.assertEqual(stc.games, 1602)
        self.assertEqual((ltc.passed, ltc.failed, ltc.pass_rate, ltc.median_games_to_fail), (1, 0, 1.0, None))
        chained = present(stc.chained_elo)
        self.assertEqual((stc.games, stc.finished_games), (1602, 1202))
        self.assertAlmostEqual(present(stc.games_per_elo), 1202 / chained.value)

    def test_games_per_elo_needs_a_positive_chain(self):
        rows = Rows()
        rows.run('a', 'b', penta=WEAK)
        rows.run('b', 'c', penta=WEAK)
        report = economics(present(build_lineage(rows.steps())), None, self.start, self.end)
        self.assertLess(present(report.classes[0].chained_elo).value, 0)
        self.assertIsNone(report.classes[0].games_per_elo)

    def test_speed_is_chained_over_the_window(self):
        self.assertAlmostEqual(present(self.report.speed.total).ratio, 0.98 * 0.99)
        self.assertEqual([point.index for point in self.report.speed.points], [1, 2])

    def test_cadence(self):
        cadence = self.report.cadence
        self.assertEqual(cadence.joined, 2)
        self.assertEqual(
            [(week.week_start, week.steps) for week in cadence.weekly],
            [(date(2026, 8, 24), 0), (date(2026, 8, 31), 1), (date(2026, 9, 7), 1), (date(2026, 9, 14), 0)],
        )
        self.assertAlmostEqual(present(cadence.steps_per_week), 7 * 2 / 14)
        self.assertEqual(cadence.acceptance_samples, 2)
        self.assertEqual(cadence.median_acceptance_seconds, (29 * 3600 + 7 * 3600) / 2)
        self.assertEqual(cadence.mean_acceptance_seconds, (29 * 3600 + 7 * 3600) / 2)
        self.assertEqual((cadence.confirmation_samples, cadence.median_confirmation_seconds), (1, 26 * 3600.0))

    def test_the_window_keeps_its_suffix_and_the_candidates_off_its_origin(self):
        since = START + timedelta(days=5)
        report = economics(self.lineage, since, date(2026, 9, 6), self.end)
        self.assertEqual((report.trunk.steps, report.failed.steps, report.other.steps), (1, 1, 1))
        self.assertEqual([point.index for point in report.speed.points], [2])
        self.assertAlmostEqual(present(report.speed.total).ratio, 0.99)

    def test_a_trunk_step_built_on_without_a_pass_still_counts_in_the_velocity(self):
        rows = Rows()
        rows.run('a', 'b', status=RunStatus.FAILED, penta=WEAK, created=START)
        rows.run('b', 'c', created=START + timedelta(days=1))
        report = economics(present(build_lineage(rows.steps())), None, self.start, self.end)
        self.assertEqual((report.cadence.joined, report.cadence.acceptance_samples), (2, 1))
        self.assertEqual(report.failed.steps, 0)

    def test_an_empty_window(self):
        rows = Rows()
        rows.run('a', 'b', status=RunStatus.FAILED, penta=WEAK)
        report = economics(present(build_lineage(rows.steps())), None, self.start, self.end)
        self.assertEqual((report.trunk.steps, report.failed.steps), (0, 1))
        self.assertIsNone(report.trunk.core_share)
        self.assertIsNone(report.cadence.steps_per_week)
        self.assertIsNone(report.speed.total)
        page = economics_page(report)
        self.assertEqual(page.tiles[0].value, '—')

    def test_page(self):
        page = economics_page(self.report)
        tiles = {tile.label: tile for tile in page.tiles}
        self.assertEqual(tiles['Speed along the trunk'].value, '−3.0%')
        self.assertIn('no interval: 1 step without host spread', tiles['Speed along the trunk'].meta)
        self.assertEqual(len(page.tiles), 6)
        self.assertEqual(tiles['Spent on failed changes'].value, '16%')
        self.assertIn('estimated: counters cover 80% of games', tiles['Spent on failed changes'].meta)
        self.assertIn('20% of games · 1 of 4 tested changes', tiles['Spent on failed changes'].meta)
        self.assertEqual(tiles['Trunk velocity'].value, '1.0 / week')
        self.assertEqual(tiles['Trunk velocity'].meta, '2 settled steps in 14 days')
        self.assertEqual(tiles['First test to accepted'].value, '18.0 h')
        self.assertIn('STC pass to LTC pass 26.0 h (1)', tiles['First test to accepted'].meta)
        self.assertIn('1,202 games of finished tests', tiles['Games per Elo · STC'].meta)
        self.assertIn('optimistic', tiles['Games per Elo · STC'].meta)
        self.assertIn('1 other candidate: 400 games, about 2.7 core-h (estimated; counters cover 0%', page.spending)
        self.assertEqual([row.label for row in page.classes], ['STC', 'LTC'])
        self.assertEqual([row.position for row in page.speed], ['2', '1'])
        self.assertIn('Node counters cover 80% of games', page.spending)


class LiveShapeTests(SimpleTestCase):
    def setUp(self):
        rows = Rows()
        hour = timedelta(hours=1)
        hosts = (host('x', 0.99, games=200), host('y', 0.99, games=200))
        rows.run('a', 'b', hosts, created=START, hours=2)
        rows.run('a', 'b', hosts, time_class=TimeClass.LTC, created=START + 4 * hour, hours=5)
        rows.run('b', 'c', hosts, created=START + 12 * hour, hours=3)
        rows.run(
            'b', 'c', hosts, status=RunStatus.RUNNING, time_class=TimeClass.LTC, penta=STRONG, created=START + 17 * hour
        )
        self.lineage = present(build_lineage(rows.steps()))
        self.report = economics(self.lineage, None, date(2026, 9, 1), date(2026, 9, 2))

    def test_a_running_confirmation_adds_neither_elo_nor_games_to_games_per_elo(self):
        stc, ltc = self.report.classes
        self.assertEqual((ltc.games, ltc.finished_games), (800, 400))
        self.assertAlmostEqual(present(ltc.games_per_elo), 400 / present(ltc.chained_elo).value)
        self.assertEqual((stc.games, stc.finished_games), (800, 800))

    def test_a_failed_candidate_still_pays_into_games_per_elo(self):
        rows = Rows()
        rows.run('a', 'b')
        rows.run('a', 'f', status=RunStatus.FAILED, penta=WEAK)
        report = economics(present(build_lineage(rows.steps())), None, date(2026, 9, 1), date(2026, 9, 9))
        self.assertEqual(report.classes[0].finished_games, 802)

    def test_a_step_whose_confirmation_is_running_is_not_accepted_yet(self):
        cadence = self.report.cadence
        self.assertEqual((cadence.joined, cadence.acceptance_samples), (1, 1))
        self.assertEqual(cadence.median_acceptance_seconds, 10 * 3600)
        self.assertEqual(sum(week.steps for week in cadence.weekly), 1)

    def test_a_young_lineage_has_a_count_not_a_weekly_rate(self):
        cadence = self.report.cadence
        self.assertEqual((cadence.span_days, cadence.steps_per_week), (2, None))
        tile = velocity_tile(cadence)
        self.assertEqual(tile.value, '1 step in 2 days')
        week = economics(self.lineage, None, date(2026, 9, 1), date(2026, 9, 7)).cadence
        self.assertEqual((week.span_days, week.steps_per_week), (7, 1.0))


class EstimatedHoursTests(SimpleTestCase):
    def report(self, failed_hosts: tuple[HostCounters, ...], time_class: TimeClass = TimeClass.STC):
        rows = Rows()
        rows.run('a', 'b', (host('x'),))
        rows.run('a', 'f', failed_hosts, status=RunStatus.FAILED, penta=PENTA, time_class=time_class)
        return economics(present(build_lineage(rows.steps())), None, date(2026, 9, 1), date(2026, 9, 9))

    def test_a_failed_candidate_without_counters_is_estimated_at_its_class_rate(self):
        report = self.report((uncounted('x'),))
        self.assertEqual((report.failed.core_hours, report.failed.estimated_core_hours), (0.0, 2.0))
        self.assertEqual(report.failed.core_share, 0.5)
        self.assertTrue(report.core_hours_estimated)
        tile = failed_tile(report)
        self.assertEqual(tile.value, '50%')
        self.assertIn('estimated', tile.meta)

    def test_partial_counters_scale_to_the_run(self):
        report = self.report((host('x', games=100, ms=MS_PER_HOUR // 4), uncounted('y', games=300)))
        self.assertEqual((report.failed.core_hours, report.failed.estimated_core_hours), (0.5, 2.0))

    def test_no_rate_for_the_class_falls_back_to_the_games_share(self):
        report = self.report((uncounted('x'),), TimeClass.LTC)
        self.assertIsNone(report.failed.estimated_core_hours)
        self.assertIsNone(report.failed.core_share)
        tile = failed_tile(report)
        self.assertEqual((tile.value, tile.meta), ('50%', 'of games (no node counters) · 1 of 2 tested changes'))

    def test_full_counters_are_not_called_an_estimate(self):
        report = self.report((host('x'),))
        self.assertFalse(report.core_hours_estimated)
        self.assertEqual(failed_tile(report).meta, 'of search time; 50% of games · 1 of 2 tested changes')


class EffectiveFreedomTests(SimpleTestCase):
    WEIGHTS = ((1, 1, 1, 1, 1), (4, 2, 2, 1, 1, 1), (1, 2, 4, 8, 16, 32), (3, 2), (40, 1, 1, 1, 1, 1, 1, 1, 1))
    TRIALS = 1500

    def covered(self, weights: tuple[int, ...], rng: random.Random) -> bool | None:
        hosts = [
            host(str(index), math.exp(rng.gauss(0.0, 0.01 / math.sqrt(weight))), ms=weight * MS_PER_HOUR)
            for index, weight in enumerate(weights)
        ]
        speed = present(speed_of(hosts))
        if speed.lower is None or speed.upper is None:
            return None
        return speed.lower <= 1.0 <= speed.upper

    def test_intervals_cover_the_truth_under_unequal_host_times(self):
        rng = random.Random(20261001)
        for weights in self.WEIGHTS:
            outcomes = [self.covered(weights, rng) for _ in range(self.TRIALS)]
            bounded = [found for found in outcomes if found is not None]
            with self.subTest(weights=weights):
                if bounded:
                    self.assertGreaterEqual(sum(bounded) / len(bounded), 0.93)

    def test_a_dominant_host_leaves_no_interval(self):
        speed = present(speed_of([host('m4', 0.98, ms=40 * MS_PER_HOUR), *(host(str(i), 0.97) for i in range(8))]))
        self.assertEqual((speed.hosts, speed.lower), (9, None))
        rows = Rows()
        rows.run('a', 'b', (host('m4', 0.98, ms=40 * MS_PER_HOUR), host('b', 0.97)))
        self.assertIn('one host dominates, no interval', speed_text(rows.step().speed))

    def test_unequal_hosts_widen_the_interval(self):
        even = present(speed_of([host('a', 0.96), host('b', 1.0), host('c', 0.98)]))
        uneven = present(speed_of([host('a', 0.96, ms=3 * MS_PER_HOUR), host('b', 1.0), host('c', 0.98)]))
        self.assertGreater(present(uneven.upper) / present(uneven.lower), present(even.upper) / present(even.lower))


class SideThreadsTests(SimpleTestCase):
    def test_each_side_is_charged_its_own_threads(self):
        rows = Rows()
        rows.run('a', 'b', (host('x'),), time_class=TimeClass.OTHER, threads=4, base_threads=1)
        self.assertEqual(rows.step().cost.core_hours, 4 + 1)
