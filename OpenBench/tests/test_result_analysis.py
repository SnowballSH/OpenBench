import itertools
import math
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from unittest import mock

import numpy as np
from django.test import SimpleTestCase

from OpenBench.fleet.hosts import HostKey
from OpenBench.insights.contributions import ResultRow
from OpenBench.insights.domain import Outcomes, SprtBounds, WorkloadFacts, WorkloadMode, WorkloadStatus
from OpenBench.insights.results.analysis import analyse_results
from OpenBench.insights.results.breakdown import outcome_breakdown
from OpenBench.insights.results.consistency import MIN_GROUP_SAMPLES, check_consistency, heterogeneity
from OpenBench.insights.results.hosts import group_by_cpu, group_by_host
from OpenBench.insights.results.outlook import (
    DriftPosterior,
    ExitTimeLaw,
    GamesRange,
    Prior,
    SprtOutlook,
    Walk,
    drift_posterior,
    forecast,
    gather_evidence,
    pass_probability,
    prior_sd,
    sprt_outlook,
)
from OpenBench.insights.results.speeds import compare_speed
from OpenBench.insights.results.verdict import VerdictKind, VerdictTone, games_text, give_verdict, percent_text
from OpenBench.insights.serialize import to_json
from OpenBench.insights.speed import SpeedCounters
from OpenBench.insights.sprt import LlrIncrement, expected_exit, llr_increment
from OpenBench.insights.strength import elo_interval, likelihood_of_superiority, summarize_strength
from OpenBench.insights.workload import build_insights
from OpenBench.stats import MLE_tvalue, PentanomialSPRT, TrinomialSPRT
from OpenBench.tests.fixtures import present

T0 = datetime(2026, 9, 1, tzinfo=UTC)
BOUNDS = SprtBounds(elo0=0.0, elo1=3.0, lower_llr=-2.94, upper_llr=2.94)
PENTA = (253, 1245, 2113, 1341, 248)
LEVEL = (50, 250, 400, 250, 50)
NO_SPEED = SpeedCounters()


def outcomes(penta=PENTA, use_penta=True) -> Outcomes:
    wins = 2 * penta[4] + penta[3]
    losses = 2 * penta[0] + penta[1]
    return Outcomes((losses, 2 * sum(penta) - wins - losses, wins), tuple(penta), use_penta)


def facts(
    penta=PENTA, mode=WorkloadMode.SPRT, status=WorkloadStatus.ACTIVE, target=None, use_penta=True
) -> WorkloadFacts:
    return WorkloadFacts(
        id=1,
        mode=mode,
        status=status,
        created_at=T0,
        updated_at=T0,
        finished=status != WorkloadStatus.ACTIVE,
        outcomes=outcomes(penta, use_penta),
        llr=PentanomialSPRT(penta, BOUNDS.elo0, BOUNDS.elo1),
        sprt=BOUNDS if mode == WorkloadMode.SPRT else None,
        target_games=target,
    )


def row(machine_id, penta, name='box', owner='lab', cpu='Ryzen', crashes=0, timelosses=0, speed=NO_SPEED, host=None):
    key = HostKey(host or f'{owner}/{name}/{cpu}')
    return ResultRow(machine_id, key, name, owner, cpu, outcomes(penta), crashes, timelosses, speed)


def counters(dev_nps: int, base_nps: int, seconds: int = 100, scale: float = 1.0) -> SpeedCounters:
    time_ms = 1000 * seconds
    scaled = round(time_ms / scale)
    return SpeedCounters(dev_nps * seconds, time_ms, scaled, base_nps * seconds, time_ms, scaled)


def cpus_of(rows):
    return group_by_cpu(group_by_host(rows, use_penta=True), use_penta=True)


def consistency_of(rows):
    return check_consistency(cpus_of(rows))


def speed_of(rows):
    cpus = cpus_of(rows)
    total = [sum(values) for values in zip(*(r.speed.as_tuple() for r in rows), strict=True)]
    return compare_speed(SpeedCounters(*total), cpus)


def verdict_for(penta=PENTA, outlook=None, **kwargs):
    workload = facts(penta, **kwargs)
    return give_verdict(workload, summarize_strength(workload.outcomes), outlook)


WALK_VARIANCE = 4e-4


def law_for(drift: float, start: float, variance: float = WALK_VARIANCE) -> ExitTimeLaw:
    return ExitTimeLaw(DriftPosterior(np.array([drift]), np.array([1.0])), Walk(variance, start, -2.94, 2.94))


class BreakdownTests(SimpleTestCase):
    def test_fractions_and_rates(self):
        breakdown = outcome_breakdown(outcomes((10, 20, 40, 20, 10)))
        self.assertEqual(breakdown.pentanomial_fractions, (0.1, 0.2, 0.4, 0.2, 0.1))
        self.assertEqual(breakdown.trinomial_fractions, (0.2, 0.6, 0.2))
        self.assertEqual(breakdown.swept_pair_rate, 0.2)
        self.assertEqual(breakdown.level_pair_rate, 0.4)
        self.assertEqual(breakdown.decisive_game_rate, 0.4)
        self.assertEqual(breakdown.games_per_decisive, 2.5)

    def test_independent_games_have_a_variance_ratio_of_one(self):
        loss, draw, win = 0.2, 0.5, 0.3
        pairs = (loss * loss, 2 * loss * draw, draw * draw + 2 * win * loss, 2 * win * draw, win * win)
        LL, LD, DD, DW, WW = (round(20_000 * p) for p in pairs)
        results = Outcomes((8_000, 20_000, 12_000), (LL, LD, DD, DW, WW), True)
        variance = present(outcome_breakdown(results).pair_variance)
        self.assertAlmostEqual(variance.ratio, 1.0, places=9)
        self.assertAlmostEqual(variance.game_correlation, 0.0, places=9)
        self.assertAlmostEqual(variance.pair_efficiency, 1.0, places=9)

    def test_pairs_that_split_a_biased_opening_reduce_the_variance(self):
        split = Outcomes((70, 60, 70), (5, 10, 70, 10, 5), True)
        variance = present(outcome_breakdown(split).pair_variance)
        self.assertLess(variance.ratio, 0.5)
        self.assertLess(variance.game_correlation, 0.0)
        self.assertAlmostEqual(variance.pair_efficiency, variance.independent / variance.observed)
        self.assertGreater(variance.pair_efficiency, 2.0)

    def test_undefined_without_games_or_spread(self):
        empty = outcome_breakdown(outcomes((0, 0, 0, 0, 0)))
        self.assertEqual(
            (empty.pentanomial_fractions, empty.trinomial_fractions, empty.games_per_decisive, empty.pair_variance),
            (None, None, None, None),
        )
        self.assertIsNone(outcome_breakdown(outcomes((0, 0, 50, 0, 0))).pair_variance)

    def test_counters_that_disagree_have_no_variance_ratio(self):
        self.assertIsNone(outcome_breakdown(Outcomes((10, 20, 11), (2, 5, 6, 5, 2), True)).pair_variance)


class HostGroupingTests(SimpleTestCase):
    def test_machine_rows_of_one_host_are_merged(self):
        rows = [
            row(1, (1, 2, 3, 2, 1), crashes=1, speed=counters(1000, 900)),
            row(7, (0, 1, 2, 1, 0), timelosses=2, speed=counters(1000, 900)),
            row(3, (0, 1, 1, 1, 0), name='other'),
        ]
        first, second = group_by_host(rows, use_penta=True)
        self.assertEqual((first.machine_ids, first.newest_machine_id), ((1, 7), 7))
        self.assertEqual(first.tally.outcomes.pentanomial, (1, 3, 5, 3, 1))
        self.assertEqual((first.tally.crashes, first.tally.timelosses), (1, 2))
        self.assertEqual(first.tally.speed.dev_nodes, 200_000)
        self.assertEqual(second.machine_name, 'other')

    def test_the_host_key_decides_the_grouping_and_the_newest_row_names_it(self):
        uuid = 'a0741747-7d81-49a2-b96e-4b14d4c304c3'
        rows = [
            row(1, LEVEL, name='old-name', host='one'),
            row(2, LEVEL, name=f'batch-{uuid}:0', host='one'),
            row(3, LEVEL, name=f'batch-{uuid}:0', host='two', cpu=None),
        ]
        first, second = group_by_host(rows, use_penta=True)
        self.assertEqual((first.key, first.machine_ids), ('one', (1, 2)))
        self.assertEqual((first.machine_name, first.machine_label), (f'batch-{uuid}:0', 'batch-a0741747…:0'))
        self.assertEqual(first.pool, 'batch-*')
        self.assertEqual((second.key, second.cpu_name), ('two', 'Unknown'))

    def test_cpus_collect_their_hosts(self):
        rows = [row(1, LEVEL, name='a'), row(2, LEVEL, name='b'), row(3, (1, 1, 1, 1, 1), cpu='Apple M4')]
        ryzen, apple = cpus_of(rows)
        self.assertEqual((ryzen.cpu_name, len(ryzen.hosts), ryzen.tally.outcomes.pairs), ('Ryzen', 2, 2000))
        self.assertEqual((apple.cpu_name, len(apple.hosts), apple.tally.outcomes.pairs), ('Apple M4', 1, 5))

    def test_no_rows(self):
        self.assertEqual(cpus_of([]), [])


class SpeedTests(SimpleTestCase):
    def test_overall_difference_and_scaled_speeds(self):
        speed = present(speed_of([row(1, LEVEL, speed=counters(1_020_000, 1_000_000, scale=2.0))]))
        overall = speed.overall.speed
        self.assertEqual((overall.dev_nps, overall.base_nps), (1_020_000, 1_000_000))
        self.assertEqual((overall.dev_nps_scaled, overall.base_nps_scaled), (2_040_000, 2_000_000))
        self.assertAlmostEqual(present(overall.difference), 0.02)
        self.assertAlmostEqual(speed.overall.difference, 0.02)
        self.assertIsNone(speed.overall.spread)

    def test_a_consistent_difference_is_beyond_noise(self):
        rows = [
            row(index, LEVEL, name=f'host-{index}', speed=counters(1_000_000 + 20_000 + 500 * index, 1_000_000))
            for index in range(6)
        ]
        spread = present(present(speed_of(rows)).overall.spread)
        self.assertEqual(spread.hosts, 6)
        self.assertTrue(spread.beyond_noise)
        self.assertLess(spread.lower, spread.mean)
        self.assertGreater(spread.lower, 0.015)

    def test_a_difference_that_changes_sign_between_hosts_is_noise(self):
        speeds = (1_030_000, 980_000, 1_010_000, 995_000)
        rows = [row(i, LEVEL, name=f'host-{i}', speed=counters(dev, 1_000_000)) for i, dev in enumerate(speeds)]
        spread = present(present(speed_of(rows)).overall.spread)
        self.assertFalse(spread.beyond_noise)
        self.assertLess(spread.lower, 0.0)
        self.assertGreater(spread.upper, 0.0)

    def test_each_cpu_has_its_own_reading(self):
        rows = [
            row(1, LEVEL, name='a', speed=counters(2_100_000, 2_000_000)),
            row(2, LEVEL, name='b', speed=counters(2_110_000, 2_000_000)),
            row(3, LEVEL, cpu='Apple M4', speed=counters(900_000, 1_000_000, seconds=10)),
            row(4, LEVEL, cpu='Silent'),
        ]
        speed = present(speed_of(rows))
        self.assertEqual([cpu.cpu_name for cpu in speed.cpus], ['Ryzen', 'Apple M4'])
        ryzen, apple = speed.cpus
        self.assertEqual(present(ryzen.reading.spread).hosts, 2)
        self.assertAlmostEqual(apple.reading.difference, -0.1)
        self.assertIsNone(apple.reading.spread)

    def test_the_headline_is_the_estimate_that_is_tested(self):
        rows = [
            row(1, LEVEL, name='long', speed=counters(980_000, 1_000_000, seconds=5000)),
            row(2, LEVEL, name='a', speed=counters(1_020_000, 1_000_000, seconds=10)),
            row(3, LEVEL, name='b', speed=counters(1_021_000, 1_000_000, seconds=10)),
            row(4, LEVEL, name='c', speed=counters(1_019_000, 1_000_000, seconds=10)),
        ]
        overall = present(speed_of(rows)).overall
        spread = present(overall.spread)
        self.assertLess(present(overall.speed.difference), -0.019)
        self.assertEqual(overall.difference, spread.mean)
        self.assertGreater(overall.difference, 0.0)
        self.assertLess(spread.lower, overall.difference)
        self.assertGreater(spread.upper, overall.difference)

    def test_identical_hosts_do_not_prove_anything(self):
        rows = [row(i, LEVEL, name=f'host-{i}', speed=counters(1_020_000, 1_000_000)) for i in range(2)]
        spread = present(present(speed_of(rows)).overall.spread)
        self.assertEqual((spread.hosts, spread.beyond_noise), (2, False))
        self.assertAlmostEqual(spread.mean, 0.02)

    def test_no_counters_means_no_comparison(self):
        self.assertIsNone(speed_of([row(1, LEVEL)]))
        self.assertIsNone(compare_speed(SpeedCounters(), []))
        self.assertIsNone(speed_of([row(1, LEVEL, speed=SpeedCounters(dev_nodes=5, dev_time=5))]))


def sampled(rng: np.random.Generator, pairs: int, pdf=(0.05, 0.24, 0.42, 0.24, 0.05)) -> tuple[int, ...]:
    return tuple(int(n) for n in rng.multinomial(pairs, pdf))


class ConsistencyTests(SimpleTestCase):
    def test_agreeing_cpus_are_not_flagged(self):
        consistency = consistency_of([row(1, LEVEL, name='a'), row(2, LEVEL, cpu='Apple M4')])
        test = present(consistency.heterogeneity)
        self.assertEqual((test.statistic, test.degrees_of_freedom, test.p_value, test.flagged), (0.0, 1, 1.0, False))
        self.assertEqual([cpu.stats.deviation.z_score for cpu in consistency.cpus if cpu.stats.deviation], [0.0, 0.0])
        self.assertEqual(consistency.hosts.flagged, [])
        self.assertEqual((consistency.hosts.total, consistency.hosts.tested), (2, 2))

    def test_a_cpu_that_disagrees_is_flagged(self):
        rows = [row(1, (500, 2400, 4200, 2400, 500)), row(2, (20, 150, 400, 300, 130), cpu='Apple M4')]
        consistency = consistency_of(rows)
        self.assertTrue(present(consistency.heterogeneity).flagged)
        apple = next(cpu for cpu in consistency.cpus if cpu.cpu_name == 'Apple M4')
        deviation = present(apple.stats.deviation)
        self.assertGreater(deviation.z_score, 4.0)
        self.assertTrue(deviation.flagged)
        self.assertEqual(deviation.adjusted_p_value, min(1.0, 2 * deviation.p_value))

    def test_small_groups_are_not_tested_alone_but_pooled(self):
        small = (5, 25, 40, 25, 5)
        rows = [row(1, (500, 2400, 4200, 2400, 500))] + [
            row(index, small, name=f'tiny-{index}', cpu=f'cpu-{index}') for index in range(2, 6)
        ]
        consistency = consistency_of(rows)
        self.assertLess(sum(small), MIN_GROUP_SAMPLES)
        self.assertEqual([cpu.stats.deviation for cpu in consistency.cpus[1:]], [None] * 4)
        test = present(consistency.heterogeneity)
        self.assertEqual((test.degrees_of_freedom, test.pooled_small_groups), (1, 4))
        self.assertEqual(consistency.hosts.tested, 1)

    def test_too_little_data_to_compare(self):
        self.assertIsNone(consistency_of([row(1, LEVEL)]).heterogeneity)
        self.assertIsNone(consistency_of([row(1, LEVEL), row(2, (1, 1, 1, 1, 1), cpu='x')]).heterogeneity)
        self.assertIsNone(heterogeneity([(0, 0, 500, 0, 0), (0, 0, 500, 0, 0)]))
        empty = consistency_of([])
        self.assertEqual((empty.cpus, empty.heterogeneity, empty.hosts.total), ([], None, 0))

    def test_a_host_that_deviates_implausibly_is_listed(self):
        rng = np.random.default_rng(5)
        rows = [row(i, sampled(rng, 400), name=f'batch-{i}') for i in range(40)]
        rows.append(row(99, sampled(rng, 400, (0.01, 0.09, 0.30, 0.35, 0.25)), name='broken'))
        flagged = consistency_of(rows).hosts.flagged
        self.assertEqual([host.machine_name for host in flagged], ['broken'])
        self.assertTrue(present(flagged[0].stats.deviation).flagged)

    def test_many_small_honest_hosts_rarely_raise_a_flag(self):
        rng = np.random.default_rng(11)
        workloads = 80
        flagged = 0
        for _ in range(workloads):
            rows = [
                row(i, sampled(rng, int(rng.integers(40, 600))), name=f'batch-{i}', cpu=f'EPYC {i % 2}')
                for i in range(120)
            ]
            consistency = consistency_of(rows)
            flagged += bool(consistency.hosts.flagged) or present(consistency.heterogeneity).flagged
        self.assertLessEqual(flagged, 1)

    def test_crashes_and_time_losses_are_flagged_by_rate(self):
        rows = [
            row(1, LEVEL, name='clean'),
            row(2, LEVEL, name='crashy', crashes=3),
            row(3, LEVEL, name='slow', timelosses=11),
            row(4, LEVEL, name='unlucky', timelosses=1),
            row(5, (500, 2500, 4000, 2500, 500), name='big', crashes=2, timelosses=10),
            row(6, (5, 25, 40, 25, 5), name='one-off', crashes=1),
        ]
        consistency = consistency_of(rows)
        flags = {
            host.machine_name: (host.stats.crash_flagged, host.stats.timeloss_flagged)
            for host in consistency.hosts.flagged
        }
        self.assertEqual(flags, {'crashy': (True, False), 'slow': (False, True)})
        crashy = next(host for host in consistency.hosts.flagged if host.machine_name == 'crashy')
        self.assertEqual((crashy.stats.crashes, crashy.stats.crash_rate), (3, 3 / 2000))
        self.assertEqual((crashy.machine_label, crashy.pool), ('crashy', 'crashy'))
        self.assertEqual(consistency.cpus[0].stats.crashes, 6)

    def test_rates_are_undefined_without_games(self):
        stats = consistency_of([row(1, (0, 0, 0, 0, 0), crashes=2, timelosses=2)]).cpus[0].stats
        self.assertEqual((stats.crash_rate, stats.timeloss_rate, stats.elo), (None, None, None))
        self.assertEqual((stats.crash_flagged, stats.timeloss_flagged), (False, False))

    def test_only_flagged_hosts_get_their_statistics_built(self):
        rows = [row(i, LEVEL, name=f'batch-{i}') for i in range(50)] + [row(99, LEVEL, name='crashy', crashes=5)]
        with mock.patch('OpenBench.insights.results.consistency.elo_interval', wraps=elo_interval) as built:
            consistency = consistency_of(rows)
        self.assertEqual((consistency.hosts.total, len(consistency.hosts.flagged)), (51, 1))
        self.assertEqual(built.call_count, len(consistency.cpus) + 1)


def tilted_law(tilt: float, start: float) -> ExitTimeLaw:
    return law_for(tilt * WALK_VARIANCE / (2 * 2.94), start)


def simulated_exit_steps(
    rng: np.random.Generator, drift: float, start: float, walks: int = 4000, stride: int = 1
) -> np.ndarray:
    position = np.full(walks, float(start))
    steps = np.zeros(walks)
    alive = np.ones(walks, dtype=bool)
    while alive.any():
        position[alive] += stride * drift + math.sqrt(stride * WALK_VARIANCE) * rng.standard_normal(int(alive.sum()))
        steps[alive] += stride
        alive &= (position > -2.94) & (position < 2.94)
    return steps


class OutlookTests(SimpleTestCase):
    def outlook(self, penta=PENTA, llr=None, bounds=BOUNDS, use_penta=True) -> SprtOutlook | None:
        results = outcomes(penta, use_penta)
        if llr is None:
            llr = PentanomialSPRT(penta, bounds.elo0, bounds.elo1)
        return sprt_outlook(results, llr, bounds)

    def test_it_is_far_less_confident_than_the_plug_in_estimate(self):
        results = outcomes()
        llr = PentanomialSPRT(PENTA, BOUNDS.elo0, BOUNDS.elo1)
        plug_in = present(expected_exit(present(llr_increment(results, BOUNDS)), llr, -2.94, 2.94))
        outlook = present(self.outlook())

        self.assertAlmostEqual(present(likelihood_of_superiority(PENTA)), 0.898, places=3)
        self.assertGreater(plug_in.pass_probability, 0.99)
        self.assertGreater(outlook.pass_probability, 0.55)
        self.assertLess(outlook.pass_probability, 0.8)
        self.assertEqual(outlook.interval, 0.8)
        self.assertEqual((outlook.prior.mean_elo, outlook.prior.sd_elo), (0.0, 4.0))
        self.assertAlmostEqual(
            outlook.prior.equivalent_games, 2 * (800 / math.log(10) / math.sqrt(2) / 4.0) ** 2, delta=2
        )

    def test_the_prior_pulls_an_early_lead_towards_no_difference(self):
        penta = (85, 440, 730, 500, 100)
        results = outcomes(penta)
        evidence = present(gather_evidence(results, BOUNDS))
        llr = PentanomialSPRT(penta, 0.0, 3.0)
        flat = forecast(evidence, llr, BOUNDS, 1e9)
        informed = present(self.outlook(penta))

        self.assertGreater(flat.pass_probability, 0.85)
        self.assertLess(informed.pass_probability, flat.pass_probability - 0.1)
        self.assertGreater(informed.remaining_games.median, 1.5 * flat.remaining_games.median)

    def test_the_prior_stops_mattering_once_the_games_outweigh_it(self):
        penta = tuple(20 * n for n in PENTA)
        evidence = present(gather_evidence(outcomes(penta), BOUNDS))
        flat = forecast(evidence, 1.0, BOUNDS, 1e9)
        informed = forecast(evidence, 1.0, BOUNDS, prior_sd(BOUNDS))
        self.assertAlmostEqual(flat.pass_probability, informed.pass_probability, delta=0.02)
        self.assertAlmostEqual(flat.remaining_games.median / informed.remaining_games.median, 1.0, delta=0.05)

    def test_the_prior_widens_with_the_bounds(self):
        widths = [prior_sd(SprtBounds(low, high, -2.94, 2.94)) for low, high in ((0, 1), (0, 3), (-3, 1), (0, 12))]
        self.assertEqual(widths[:2], [4.0, 4.0])
        self.assertAlmostEqual(widths[2], 16 / 3)
        self.assertAlmostEqual(widths[3], 16.0)

    def test_no_forecast_until_the_elo_is_known_about_as_well_as_the_bounds_are_wide(self):
        self.assertIsNone(self.outlook((1, 18, 48, 29, 4)))
        self.assertIsNone(self.outlook((80, 400, 640, 400, 80)))
        self.assertIsNotNone(self.outlook((85, 440, 730, 500, 100)))

        wide = SprtBounds(0.0, 20.0, -2.94, 2.94)
        early = (5, 30, 50, 35, 6)
        self.assertIsNotNone(self.outlook(early, PentanomialSPRT(early, 0.0, 20.0), wide))

    def test_the_range_brackets_the_median_and_counts_games(self):
        games = present(self.outlook()).remaining_games
        self.assertLess(games.lower, games.median)
        self.assertLess(games.median, games.upper)
        self.assertEqual({games.lower % 2, games.median % 2, games.upper % 2}, {0})
        self.assertGreater(games.upper / games.lower, 3.0)

    def test_a_higher_llr_is_more_likely_to_pass_and_sooner_to_end(self):
        low, high = (present(self.outlook(llr=llr)) for llr in (-2.0, 2.0))
        self.assertLess(low.pass_probability, high.pass_probability)
        near_bound = present(self.outlook(llr=2.9))
        self.assertGreater(near_bound.pass_probability, high.pass_probability)
        self.assertLess(near_bound.remaining_games.median, high.remaining_games.median)

    def test_mirrored_results_mirror_the_chance(self):
        centred = SprtBounds(-3.0, 3.0, -2.94, 2.94)
        mirrored = tuple(reversed(PENTA))
        ahead = present(self.outlook(PENTA, PentanomialSPRT(PENTA, -3.0, 3.0), centred))
        behind = present(self.outlook(mirrored, PentanomialSPRT(mirrored, -3.0, 3.0), centred))
        self.assertAlmostEqual(ahead.pass_probability + behind.pass_probability, 1.0, places=6)
        self.assertEqual(ahead.remaining_games, behind.remaining_games)

    def test_a_clear_result_is_nearly_certain_and_short(self):
        penta = (300, 3000, 9000, 5200, 900)
        outlook = present(self.outlook(penta, llr=2.5))
        self.assertGreater(outlook.pass_probability, 0.99)
        self.assertLessEqual(outlook.pass_probability, 1.0)
        self.assertLess(outlook.remaining_games.upper, 2 * sum(penta))

    def test_trinomial_workloads_step_one_game_at_a_time(self):
        results = outcomes(PENTA, use_penta=False)
        outlook = present(sprt_outlook(results, TrinomialSPRT(results.trinomial, 0.0, 3.0), BOUNDS))
        self.assertGreater(outlook.pass_probability, 0.3)
        self.assertLess(outlook.pass_probability, 0.95)
        self.assertAlmostEqual(outlook.prior.equivalent_games, (800 / math.log(10) / 4.0) ** 2, delta=1)

    def test_undefined_cases(self):
        self.assertIsNone(self.outlook((1, 10, 60, 10, 1)))
        self.assertIsNone(self.outlook(llr=2.94))
        self.assertIsNone(self.outlook(llr=-3.5))
        self.assertIsNone(sprt_outlook(Outcomes((0, 4000, 0), (0, 0, 2000, 0, 0), True), 0.0, BOUNDS))
        self.assertIsNone(sprt_outlook(Outcomes((0, 4000, 0), (0, 0, 2000, 0, 0), False), 0.0, BOUNDS))


class ExitTimeLawTests(SimpleTestCase):
    def test_matches_simulated_walks_for_weak_and_strong_drifts(self):
        rng = np.random.default_rng(3)
        for tilt, start in ((0.0, 0.5), (3.0, -1.0), (-7.9, 2.0), (8.1, 0.0), (12.0, -2.0), (-30.0, 2.0), (60.0, 0.0)):
            law = tilted_law(tilt, start)
            steps = simulated_exit_steps(rng, tilt * WALK_VARIANCE / 5.88, start, stride=1 if abs(tilt) > 20 else 4)
            for probability in (0.1, 0.5, 0.9):
                simulated = float(np.quantile(steps, probability))
                self.assertAlmostEqual(law.quantile(probability) / simulated, 1.0, delta=0.07, msg=(tilt, start))

    def test_a_strong_drift_can_still_lose_to_the_bound_behind_it(self):
        rng = np.random.default_rng(4)
        law = tilted_law(12.0, -2.5)
        steps = simulated_exit_steps(rng, 12.0 * WALK_VARIANCE / 5.88, -2.5, walks=8000)
        early = float(np.mean(steps <= 600))
        self.assertGreater(early, 0.1)
        self.assertAlmostEqual(1.0 - law.survival(600.0), early, delta=0.03)
        self.assertAlmostEqual(law.quantile(0.9) / float(np.quantile(steps, 0.9)), 1.0, delta=0.05)

    def test_series_and_images_agree_where_they_meet(self):
        for start in (-2.5, 0.0, 2.0):
            below, above = tilted_law(7.99, start), tilted_law(8.01, start)
            for probability in (0.1, 0.5, 0.9):
                self.assertAlmostEqual(
                    below.quantile(probability) / above.quantile(probability), 1.0, delta=0.01, msg=start
                )
            for steps in (1.0, 50.0, 2_000.0, 10_000.0, 40_000.0):
                self.assertAlmostEqual(below.survival(steps), above.survival(steps), delta=0.005, msg=(start, steps))

    def test_survival_starts_at_one_and_never_rises(self):
        for tilt, start in ((0.0, 0.5), (5.0, -2.9), (-7.9, 2.0), (20.0, -2.0)):
            law = tilted_law(tilt, start)
            values = [law.survival(float(steps)) for steps in np.geomspace(1e-6, 1e6, 200)]
            self.assertAlmostEqual(values[0], 1.0, places=6)
            self.assertLess(values[-1], 1e-6)
            self.assertTrue(
                all(later <= earlier + 1e-9 for earlier, later in itertools.pairwise(values)), (tilt, start)
            )

    def test_agrees_with_the_expected_exit(self):
        for tilt, start in ((0.0, 0.5), (3.0, -1.0), (-7.9, 2.0), (25.0, -2.5)):
            law = tilted_law(tilt, start)
            grid = np.linspace(0.0, 8 * law.quantile(0.9), 4001)
            mean = float(np.trapezoid([law.survival(float(t)) for t in grid], grid))
            drift = tilt * WALK_VARIANCE / 5.88
            expected = present(expected_exit(LlrIncrement(drift, WALK_VARIANCE, 1), start, -2.94, 2.94)).steps
            self.assertAlmostEqual(mean / expected, 1.0, delta=0.01, msg=(tilt, start))

    def test_a_nearly_deterministic_walk_ends_when_its_drift_says(self):
        law = law_for(0.01, 0.0, variance=1e-10)
        for probability in (0.1, 0.5, 0.9):
            self.assertAlmostEqual(law.quantile(probability), 294.0, delta=1.0)


def log_loss(forecast: np.ndarray, happened: np.ndarray) -> float:
    clipped = np.clip(forecast, 1e-6, 1.0 - 1e-6)
    return float(-np.mean(happened * np.log(clipped) + (1.0 - happened) * np.log(1.0 - clipped)))


@dataclass(frozen=True, slots=True)
class Forecast:
    pairs: int
    outlook: SprtOutlook
    flat_probability: float
    passed: bool
    remaining_games: int


class OutlookCalibrationTests(SimpleTestCase):
    """Plays real pentanomial SPRTs at the live bounds and scores the forecasts made along the way."""

    BOUNDS = BOUNDS
    BATCH = 96
    CHECKPOINTS = (1_700, 4_000, 10_000)
    SHAPE = (0.05, 0.24, 0.42, 0.24, 0.05)

    def pair_law(self, nelo: float) -> list[float]:
        t_value = nelo * math.sqrt(2) * math.log(10) / 800
        return [p for _, p in MLE_tvalue([(i / 4, p) for i, p in enumerate(self.SHAPE)], 0.5, t_value)]

    def observe(self, counts: np.ndarray, llr: float) -> tuple[SprtOutlook, float]:
        evidence = present(gather_evidence(outcomes(tuple(int(n) for n in counts)), self.BOUNDS))
        walk = Walk(evidence.increment.variance, llr, self.BOUNDS.lower_llr, self.BOUNDS.upper_llr)
        flat = pass_probability(drift_posterior(evidence, 1e9), walk)
        return forecast(evidence, llr, self.BOUNDS, prior_sd(self.BOUNDS)), flat

    def play(self, rng: np.random.Generator, nelo: float) -> list[Forecast]:
        law = self.pair_law(nelo)
        counts = np.zeros(5, dtype=int)
        pending = list(self.CHECKPOINTS)
        seen: list[tuple[int, SprtOutlook, float]] = []

        while True:
            counts += rng.multinomial(self.BATCH, law)
            pairs = int(counts.sum())
            llr = PentanomialSPRT([int(n) for n in counts], self.BOUNDS.elo0, self.BOUNDS.elo1)
            if not self.BOUNDS.lower_llr < llr < self.BOUNDS.upper_llr:
                break
            if pending and pairs >= pending[0]:
                seen.append((pending.pop(0), *self.observe(counts, llr)))

        passed = llr >= self.BOUNDS.upper_llr
        return [Forecast(mark, outlook, flat, passed, 2 * (pairs - mark)) for mark, outlook, flat in seen]

    def assert_calibrated(self, forecasts: list[Forecast], tolerance: float) -> None:
        for mark in self.CHECKPOINTS:
            made = [item for item in forecasts if item.pairs == mark]
            self.assertGreater(len(made), 60, mark)

            chance = np.array([item.outlook.pass_probability for item in made])
            flat = np.array([item.flat_probability for item in made])
            passed = np.array([float(item.passed) for item in made])
            for chosen in (chance < 0.5, chance >= 0.5):
                if int(chosen.sum()) >= 25:
                    self.assertAlmostEqual(
                        float(chance[chosen].mean()), float(passed[chosen].mean()), delta=tolerance, msg=mark
                    )

            base_rate = np.full(len(made), passed.mean())
            self.assertLess(log_loss(chance, passed), log_loss(base_rate, passed) + 0.08, mark)
            self.assertLess(log_loss(chance, passed), log_loss(flat, passed) + 0.05, mark)

            remaining = np.array([item.remaining_games for item in made])
            median = np.array([item.outlook.remaining_games.median for item in made])
            lower = np.array([item.outlook.remaining_games.lower for item in made])
            upper = np.array([item.outlook.remaining_games.upper for item in made])
            self.assertGreater(float(np.median(remaining / median)), 0.6, mark)
            self.assertLess(float(np.median(remaining / median)), 1.9, mark)
            self.assertGreater(float(((remaining >= lower) & (remaining <= upper)).mean()), 0.6, mark)

    def test_patches_worth_about_nothing(self):
        rng = np.random.default_rng(20261001)
        forecasts = [item for _ in range(170) for item in self.play(rng, float(rng.normal(0.0, 2.0)))]
        self.assert_calibrated(forecasts, tolerance=0.25)

        early = [item for item in forecasts if item.pairs == self.CHECKPOINTS[0]]
        chance = np.array([item.outlook.pass_probability for item in early])
        flat = np.array([item.flat_probability for item in early])
        passed = np.array([float(item.passed) for item in early])
        self.assertLess(log_loss(chance, passed), 0.85 * log_loss(flat, passed))

    def test_patches_spread_well_beyond_the_bounds(self):
        rng = np.random.default_rng(20261002)
        forecasts = [item for _ in range(200) for item in self.play(rng, float(rng.uniform(-6.0, 9.0)))]
        self.assert_calibrated(forecasts, tolerance=0.25)


OUTLOOK = SprtOutlook(0.613, GamesRange(8_904, 24_114, 99_004), 0.8, Prior(0.0, 5.0, 4_826))


class VerdictTests(SimpleTestCase):
    def test_active_sprt_with_a_forecast(self):
        verdict = verdict_for(outlook=OUTLOOK)
        self.assertEqual((verdict.kind, verdict.tone), (VerdictKind.LIKELY_GAIN, VerdictTone.POSITIVE))
        self.assertEqual(
            verdict.text,
            'Likely a gain: +2.9 ± 4.4 Elo, LOS 90%. Forecast: 61% chance to pass, '
            'with roughly 24k more games needed (80% range 8.9k to 99k).',
        )

    def test_active_sprt_without_a_forecast(self):
        verdict = verdict_for((2, 22, 50, 24, 2))
        self.assertEqual(verdict.kind, VerdictKind.INCONCLUSIVE)
        self.assertTrue(verdict.text.startswith('No clear difference yet: '))
        self.assertTrue(verdict.text.endswith('There are too few games to forecast how the SPRT will end.'))

    def test_headline_follows_the_printed_los(self):
        cases = (
            ((40, 300, 700, 340, 60), VerdictKind.VERY_LIKELY_GAIN, 'Very likely a small gain: ', 'LOS above 99%'),
            ((246, 1245, 2113, 1341, 255), VerdictKind.LIKELY_GAIN, 'Likely a gain: ', None),
            ((250, 1260, 2113, 1310, 250), VerdictKind.POSSIBLE_GAIN, 'Possibly a gain: ', None),
            (LEVEL, VerdictKind.INCONCLUSIVE, 'No clear difference yet: 0.0 ± ', 'LOS 50%'),
            ((250, 1310, 2113, 1260, 250), VerdictKind.POSSIBLE_LOSS, 'Possibly a loss: −', None),
            ((60, 340, 700, 300, 40), VerdictKind.VERY_LIKELY_LOSS, 'Very likely a small loss: −', 'LOS below 1%'),
        )
        for penta, kind, start, fragment in cases:
            verdict = verdict_for(penta, outlook=OUTLOOK)
            self.assertEqual(verdict.kind, kind, verdict.text)
            self.assertTrue(verdict.text.startswith(start), verdict.text)
            if fragment:
                self.assertIn(fragment, verdict.text)

    def test_tone_needs_a_likely_result(self):
        self.assertEqual(verdict_for((250, 1260, 2113, 1310, 250)).tone, VerdictTone.NEUTRAL)
        self.assertEqual(verdict_for((90, 520, 900, 300, 30)).tone, VerdictTone.NEGATIVE)
        self.assertEqual(verdict_for(LEVEL).tone, VerdictTone.NEUTRAL)

    def test_the_size_is_what_the_whole_interval_supports(self):
        self.assertIn('Very likely a large gain: ', verdict_for((20, 200, 700, 600, 280)).text)
        self.assertIn('Very likely a moderate gain: ', verdict_for((200, 1200, 2100, 1400, 300)).text)
        self.assertIn('Very likely a small gain: ', verdict_for((40, 300, 700, 340, 60)).text)

    def test_a_wide_interval_claims_no_size(self):
        verdict = verdict_for((1, 18, 48, 29, 4))
        self.assertTrue(verdict.text.startswith('Very likely a small gain: +29.6 ± 28.1 Elo'), verdict.text)
        self.assertTrue(verdict_for((2, 20, 48, 27, 3)).text.startswith('Possibly a gain: '))

    def test_passed_and_failed_sprt(self):
        passed = verdict_for((30, 300, 900, 520, 90), status=WorkloadStatus.PASSED)
        self.assertEqual((passed.kind, passed.tone), (VerdictKind.PASSED, VerdictTone.POSITIVE))
        self.assertTrue(
            passed.text.startswith(
                'Passed: after 3.7k games the SPRT with normalized Elo bounds [0, 3] favoured its upper bound over '
                'its lower one, so dev is very unlikely to be worth less than 0 normalized Elo. Measured +'
            ),
            passed.text,
        )
        self.assertTrue(
            passed.text.endswith(
                'Elo, LOS above 99%; an SPRT stops the moment it is convinced, '
                'so this estimate is biased towards the bound it stopped at.'
            )
        )

        failed = verdict_for((90, 520, 900, 300, 30), status=WorkloadStatus.FAILED)
        self.assertEqual((failed.kind, failed.tone), (VerdictKind.FAILED, VerdictTone.NEGATIVE))
        self.assertIn('favoured its lower bound over its upper one', failed.text)
        self.assertIn('so dev is very unlikely to be worth 3 normalized Elo or more. Measured −', failed.text)

    def test_a_failed_test_can_measure_a_gain_without_contradiction(self):
        failed = verdict_for((250, 1260, 2113, 1310, 250), status=WorkloadStatus.FAILED)
        self.assertIn('Measured +1.7 ± 4.4 Elo', failed.text)
        self.assertNotIn('exaggerate', failed.text)
        self.assertIn('biased towards the bound it stopped at', failed.text)

    def test_trinomial_bounds_are_named_bayeselo(self):
        passed = verdict_for((30, 300, 900, 520, 90), status=WorkloadStatus.PASSED, use_penta=False)
        self.assertIn('the SPRT with BayesElo bounds [0, 3]', passed.text)
        self.assertIn('worth less than 0 BayesElo', passed.text)

    def test_stopped_sprt_reads_like_a_measurement(self):
        verdict = verdict_for(status=WorkloadStatus.STOPPED)
        self.assertEqual(verdict.text, 'Likely a gain: +2.9 ± 4.4 Elo, LOS 90%. Based on 10k games.')

    def test_fixed_games_workloads(self):
        running = verdict_for(LEVEL, mode=WorkloadMode.GAMES, target=40_000)
        self.assertTrue(running.text.endswith('LOS 50%. 2.0k of 40k games played.'))
        self.assertTrue(running.text.startswith('No clear difference yet: '))

        done = verdict_for(LEVEL, mode=WorkloadMode.GAMES, status=WorkloadStatus.STOPPED, target=2_000)
        self.assertTrue(done.text.startswith('No clear difference: '))
        self.assertTrue(done.text.endswith('Based on 2.0k games.'))

        untargeted = verdict_for(LEVEL, mode=WorkloadMode.GAMES)
        self.assertTrue(untargeted.text.endswith('Based on 2.0k games.'))

    def test_too_few_games(self):
        for penta in ((0, 0, 0, 0, 0), (0, 0, 1, 0, 0), (0, 0, 0, 0, 2), (1, 17, 48, 29, 4)):
            verdict = verdict_for(penta)
            self.assertEqual((verdict.kind, verdict.tone), (VerdictKind.TOO_EARLY, VerdictTone.NEUTRAL))
            self.assertEqual(verdict.text, 'No verdict yet: fewer than 200 games have been played.')

    def test_identical_results_give_no_verdict(self):
        for status in (WorkloadStatus.ACTIVE, WorkloadStatus.STOPPED):
            verdict = verdict_for((0, 0, 0, 0, 150), status=status)
            self.assertEqual(verdict.kind, VerdictKind.TOO_EARLY)
            self.assertEqual(verdict.text, 'No verdict yet: every result so far is the same.')

    def test_number_wording(self):
        self.assertEqual(
            [games_text(n) for n in (0, 999, 1_000, 9_949, 31_412, 999_400, 2_340_000)],
            ['0', '999', '1.0k', '9.9k', '31k', '999k', '2.3M'],
        )
        self.assertEqual(
            [percent_text(p) for p in (0.0, 0.004, 0.01, 0.5, 0.99, 0.9951, 1.0)],
            ['below 1%', 'below 1%', '1%', '50%', '99%', 'above 99%', 'above 99%'],
        )


class AnalysisTests(SimpleTestCase):
    ROWS = (
        row(1, (130, 620, 1050, 670, 125), name='a', speed=counters(1_020_000, 1_000_000)),
        row(2, (123, 625, 1063, 671, 123), name='b', cpu='Apple M4', speed=counters(510_000, 500_000)),
    )

    def test_active_sprt_has_every_part(self):
        workload = facts()
        results = analyse_results(workload, summarize_strength(workload.outcomes), self.ROWS)
        outlook = present(results.outlook)
        self.assertIn(f'{100 * outlook.pass_probability:.0f}% chance to pass', results.verdict.text)
        self.assertEqual(results.outcomes.pentanomial_fractions, tuple(n / sum(PENTA) for n in PENTA))
        self.assertAlmostEqual(present(results.speed).overall.difference, 0.02)
        self.assertEqual([cpu.cpu_name for cpu in results.consistency.cpus], ['Apple M4', 'Ryzen'])

    def test_finished_and_fixed_games_workloads_have_no_outlook(self):
        for workload in (facts(status=WorkloadStatus.PASSED), facts(mode=WorkloadMode.GAMES, target=40_000)):
            self.assertIsNone(analyse_results(workload, summarize_strength(workload.outcomes), ()).outlook)

    def test_payload(self):
        payload = to_json(build_insights(facts(), [], self.ROWS, T0))
        assert isinstance(payload, dict)
        results = payload['results']
        assert isinstance(results, dict)
        self.assertEqual(set(results), {'verdict', 'outcomes', 'outlook', 'speed', 'consistency'})
        verdict = results['verdict']
        assert isinstance(verdict, dict)
        self.assertEqual(set(verdict), {'kind', 'tone', 'text'})
        self.assertEqual((verdict['kind'], verdict['tone']), ('likely_gain', 'positive'))
        outlook = results['outlook']
        assert isinstance(outlook, dict)
        self.assertEqual(set(outlook), {'pass_probability', 'remaining_games', 'interval', 'prior'})
        self.assertEqual(outlook['prior'], {'mean_elo': 0.0, 'sd_elo': 4.0, 'equivalent_games': 7544})
        remaining = outlook['remaining_games']
        assert isinstance(remaining, dict)
        self.assertEqual(set(remaining), {'lower', 'median', 'upper'})

    def test_tunes_have_no_results(self):
        tune = replace(facts(mode=WorkloadMode.SPSA), sprt=None)
        self.assertIsNone(build_insights(tune, [], self.ROWS, T0).results)

    def test_no_rows(self):
        workload = facts()
        results = analyse_results(workload, summarize_strength(workload.outcomes), ())
        self.assertIsNone(results.speed)
        self.assertEqual((results.consistency.cpus, results.consistency.hosts.total), ([], 0))
        self.assertIsNotNone(results.outlook)
