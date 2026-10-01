import json
import math
from datetime import UTC, datetime, timedelta

from django.test import SimpleTestCase

from OpenBench.fleet.hosts import HostKey
from OpenBench.insights.contributions import ResultRow, summarize_contributions
from OpenBench.insights.domain import Outcomes, ProgressPoint, SprtBounds, WorkloadFacts, WorkloadMode, WorkloadStatus
from OpenBench.insights.eta import EtaKind, EtaReason, estimate_eta, sprt_unavailable_reason
from OpenBench.insights.grouping import sum_by_key
from OpenBench.insights.serialize import Json, to_json
from OpenBench.insights.series import build_series
from OpenBench.insights.server import Edge, FinishedWorkload, fleet_status, games_in_window, summarize_finished
from OpenBench.insights.sprt import (
    LlrIncrement,
    expected_exit,
    forecast_sprt,
    pentanomial_increment,
    trinomial_increment,
    upper_exit_probability,
)
from OpenBench.insights.strength import (
    draw_ratio,
    elo_interval,
    likelihood_of_superiority,
    normalized_elo,
    penta_fractions,
    summarize_strength,
)
from OpenBench.insights.timing import Rate, games_at, rate_between, summarize_timing
from OpenBench.insights.workload import build_insights
from OpenBench.stats import Elo, PentanomialSPRT, TrinomialSPRT
from OpenBench.tests.fixtures import present

T0 = datetime(2026, 9, 1, tzinfo=UTC)
BOUNDS = SprtBounds(elo0=0.0, elo1=3.0, lower_llr=-2.94, upper_llr=2.94)
PENTA = (39, 884, 2667, 924, 44)


def field(value: Json, *path: str) -> Json:
    for key in path:
        assert isinstance(value, dict)
        value = value[key]
    return value


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def outcomes(penta=PENTA, use_penta=True) -> Outcomes:
    wins = 2 * penta[4] + penta[3]
    losses = 2 * penta[0] + penta[1]
    return Outcomes((losses, 2 * sum(penta) - wins - losses, wins), tuple(penta), use_penta)


def facts(mode=WorkloadMode.SPRT, penta=PENTA, finished=False, target=None, llr=None, created=T0) -> WorkloadFacts:
    results = outcomes(penta)
    return WorkloadFacts(
        id=1,
        mode=mode,
        status=WorkloadStatus.PASSED if finished else WorkloadStatus.ACTIVE,
        created_at=created,
        updated_at=at(60),
        finished=finished,
        outcomes=results,
        llr=PentanomialSPRT(penta, BOUNDS.elo0, BOUNDS.elo1) if llr is None else llr,
        sprt=BOUNDS if mode == WorkloadMode.SPRT else None,
        target_games=target,
    )


class StrengthTests(SimpleTestCase):
    def test_elo_matches_stats_module(self):
        interval = present(elo_interval(PENTA))
        self.assertEqual((interval.lower, interval.value, interval.upper), Elo(PENTA))

    def test_zero_and_single_games_are_undefined(self):
        for results in [(0, 0, 0, 0, 0), (0, 0, 1, 0, 0), (0, 0, 0)]:
            self.assertIsNone(elo_interval(results))
            self.assertIsNone(likelihood_of_superiority(results))
            self.assertIsNone(normalized_elo(results))
        empty = outcomes((0, 0, 0, 0, 0))
        self.assertIsNone(draw_ratio(empty))
        self.assertIsNone(penta_fractions(empty))

    def test_all_draws(self):
        results = outcomes((0, 0, 50, 0, 0))
        self.assertEqual(draw_ratio(results), 1.0)
        self.assertEqual(penta_fractions(results), (0.0, 0.0, 1.0, 0.0, 0.0))
        self.assertEqual(likelihood_of_superiority(results.pentanomial), 0.5)
        self.assertIsNone(normalized_elo(results.pentanomial))
        self.assertEqual(present(elo_interval(results.pentanomial)).value, 0.0)

    def test_one_sided_results_have_certain_los(self):
        self.assertEqual(likelihood_of_superiority((0, 0, 0, 0, 10)), 1.0)
        self.assertEqual(likelihood_of_superiority((10, 0, 0, 0, 0)), 0.0)

    def test_los_is_symmetric(self):
        mirrored = tuple(reversed(PENTA))
        self.assertAlmostEqual(
            present(likelihood_of_superiority(PENTA)) + present(likelihood_of_superiority(mirrored)), 1.0
        )
        self.assertGreater(present(likelihood_of_superiority(PENTA)), 0.5)

    def test_normalized_elo_interval_brackets_estimate(self):
        interval = present(normalized_elo(PENTA))
        self.assertLess(interval.lower, interval.value)
        self.assertGreater(interval.upper, interval.value)
        self.assertAlmostEqual(interval.value - interval.lower, interval.upper - interval.value)
        self.assertGreater(interval.value, 0)

    def test_summary_uses_trinomial_when_asked(self):
        tri = outcomes(use_penta=False)
        self.assertEqual(summarize_strength(tri).elo, elo_interval(tri.trinomial))
        self.assertAlmostEqual(sum(present(summarize_strength(tri).penta_fractions)), 1.0)


class TimingTests(SimpleTestCase):
    marks = [(at(0), 0), (at(60), 600), (at(120), 600)]

    def test_games_at_interpolates_and_clamps(self):
        self.assertIsNone(games_at(self.marks, at(-1)))
        self.assertEqual(games_at(self.marks, at(30)), 300)
        self.assertEqual(games_at(self.marks, at(90)), 600)
        self.assertEqual(games_at(self.marks, at(500)), 600)
        self.assertIsNone(games_at([], at(0)))

    def test_rate_between(self):
        self.assertEqual(rate_between(self.marks, at(0), at(60)), Rate(600.0, 3600.0))
        self.assertEqual(rate_between(self.marks, at(-30), at(60)), Rate(600.0, 3600.0))
        self.assertIsNone(rate_between(self.marks, at(0), at(2)))

    def test_stalled_workload_reports_zero_recent_rate(self):
        timing = present(summarize_timing(self.marks, at(180), finished=False))
        self.assertIsNone(timing.ended_at)
        self.assertEqual(timing.elapsed_seconds, 3 * 3600)
        self.assertEqual(present(timing.recent).games_per_hour, 0.0)
        self.assertEqual(present(timing.overall).games_per_hour, 200.0)
        self.assertEqual(timing.best_rate(), timing.recent)

    def test_finished_workload_ends_at_last_mark(self):
        timing = present(summarize_timing(self.marks[:2], at(60), finished=True))
        self.assertEqual(timing.ended_at, at(60))
        self.assertEqual(present(timing.recent).games_per_hour, 600.0)

    def test_no_marks(self):
        self.assertIsNone(summarize_timing([], at(0), finished=False))


class SprtTests(SimpleTestCase):
    def test_pentanomial_drift_reproduces_the_llr(self):
        increment = pentanomial_increment(PENTA, BOUNDS.elo0, BOUNDS.elo1)
        self.assertAlmostEqual(increment.drift * sum(PENTA), PentanomialSPRT(PENTA, BOUNDS.elo0, BOUNDS.elo1))
        self.assertEqual(increment.games_per_step, 2)

    def test_trinomial_drift_reproduces_the_llr(self):
        tri = (2200, 4400, 2300)
        increment = present(trinomial_increment(tri, BOUNDS.elo0, BOUNDS.elo1))
        self.assertAlmostEqual(increment.drift * sum(tri), TrinomialSPRT(tri, BOUNDS.elo0, BOUNDS.elo1))
        self.assertIsNone(trinomial_increment((0, 10, 10), BOUNDS.elo0, BOUNDS.elo1))

    def test_zero_drift_is_the_limit_of_small_drift(self):
        zero = present(expected_exit(LlrIncrement(0.0, 0.01, 2), 0.5, -2.94, 2.94))
        tiny = present(expected_exit(LlrIncrement(1e-12, 0.01, 2), 0.5, -2.94, 2.94))
        self.assertAlmostEqual(zero.steps, (0.5 + 2.94) * (2.94 - 0.5) / 0.01)
        self.assertAlmostEqual(zero.steps, tiny.steps, places=3)
        self.assertAlmostEqual(zero.pass_probability, tiny.pass_probability)

    def test_strong_drift_approaches_distance_over_drift(self):
        up = present(expected_exit(LlrIncrement(0.5, 0.01, 2), 0.5, -2.94, 2.94))
        down = present(expected_exit(LlrIncrement(-0.5, 0.01, 2), 0.5, -2.94, 2.94))
        self.assertAlmostEqual(up.steps, (2.94 - 0.5) / 0.5)
        self.assertAlmostEqual(down.steps, (0.5 + 2.94) / 0.5)
        self.assertEqual(up.pass_probability, 1.0)
        self.assertAlmostEqual(down.pass_probability, 0.0)

    def test_small_negative_drift_keeps_precision(self):
        lower, start, upper = -2.94, 0.5, 2.94
        k = -1e-8 / (upper - lower)
        linear = (start - lower) / (upper - lower)
        self.assertAlmostEqual(
            upper_exit_probability(k, start, lower, upper), linear * (1 + k * (upper - start) / 2), places=13
        )

    def test_extreme_drift_does_not_overflow(self):
        for drift in (-100.0, 100.0):
            estimate = present(expected_exit(LlrIncrement(drift, 1e-6, 2), 0.0, -2.94, 2.94))
            self.assertTrue(math.isfinite(estimate.steps))

    def test_undefined_cases(self):
        self.assertIsNone(expected_exit(LlrIncrement(0.1, 0.0, 2), 0.5, -2.94, 2.94))
        self.assertIsNone(expected_exit(LlrIncrement(0.1, 0.01, 2), 3.0, -2.94, 2.94))
        self.assertIsNone(expected_exit(LlrIncrement(0.1, 0.01, 2), -2.94, -2.94, 2.94))
        self.assertIsNone(forecast_sprt(outcomes((0, 10, 20, 10, 0)), 0.1, BOUNDS))

    def test_forecast_counts_games(self):
        forecast = present(forecast_sprt(outcomes(), PentanomialSPRT(PENTA, 0.0, 3.0), BOUNDS))
        self.assertGreater(forecast.remaining_games, 0)
        self.assertEqual(forecast.remaining_games % 2, 0)
        self.assertGreater(forecast.drift_per_game, 0)


class EtaTests(SimpleTestCase):
    rate = Rate(games_per_hour=1000.0, window_seconds=3600.0)

    def test_finished(self):
        eta = estimate_eta(facts(finished=True), self.rate, at(0))
        self.assertEqual((eta.kind, eta.remaining_games, eta.remaining_seconds), (EtaKind.FINISHED, 0, 0.0))

    def test_target_modes(self):
        eta = estimate_eta(facts(WorkloadMode.GAMES, target=20000), self.rate, at(0))
        played = outcomes().games
        self.assertEqual(eta.kind, EtaKind.TARGET)
        self.assertEqual(eta.remaining_games, 20000 - played)
        remaining_seconds = present(eta.remaining_seconds)
        self.assertAlmostEqual(remaining_seconds, 3.6 * (20000 - played))
        self.assertEqual(eta.completes_at, at(0) + timedelta(seconds=remaining_seconds))

    def test_overshoot_clamps_to_zero(self):
        self.assertEqual(estimate_eta(facts(WorkloadMode.DATAGEN, target=10), self.rate, at(0)).remaining_games, 0)

    def test_no_rate_leaves_time_unknown(self):
        for rate in (None, Rate(0.0, 3600.0)):
            eta = estimate_eta(facts(WorkloadMode.SPSA, target=20000), rate, at(0))
            self.assertIsNotNone(eta.remaining_games)
            self.assertIsNone(eta.remaining_seconds)
            self.assertIsNone(eta.completes_at)

    def test_sprt_estimate(self):
        eta = estimate_eta(facts(), self.rate, at(0))
        self.assertEqual(eta.kind, EtaKind.SPRT)
        self.assertIsNotNone(eta.completes_at)
        self.assertAlmostEqual(present(eta.remaining_seconds), 3.6 * present(eta.remaining_games))

    def test_sprt_without_enough_games(self):
        eta = estimate_eta(facts(penta=(0, 1, 2, 1, 0)), self.rate, at(0))
        self.assertEqual((eta.kind, eta.remaining_games), (EtaKind.UNAVAILABLE, None))

    def test_unreachable_completion_time_is_null(self):
        eta = estimate_eta(facts(WorkloadMode.GAMES, target=10**9), Rate(1e-9, 3600.0), at(0))
        self.assertGreater(present(eta.remaining_seconds), 1e15)
        self.assertIsNone(eta.completes_at)

    def test_missing_target(self):
        eta = estimate_eta(facts(WorkloadMode.SPSA), self.rate, at(0))
        self.assertEqual((eta.kind, eta.reason), (EtaKind.UNAVAILABLE, EtaReason.NO_TARGET))

    def test_complete_estimates_carry_no_reason(self):
        for eta in (
            estimate_eta(facts(), self.rate, at(0)),
            estimate_eta(facts(finished=True), self.rate, at(0)),
            estimate_eta(facts(WorkloadMode.GAMES, target=20000), self.rate, at(0)),
        ):
            self.assertIsNone(eta.reason)

    def test_missing_rate_is_the_reason(self):
        for rate in (None, Rate(0.0, 3600.0)):
            self.assertEqual(estimate_eta(facts(), rate, at(0)).reason, EtaReason.NO_RATE)
            self.assertEqual(
                estimate_eta(facts(WorkloadMode.GAMES, target=20000), rate, at(0)).reason, EtaReason.NO_RATE
            )

    def test_sprt_unavailable_reasons(self):
        flat = SprtBounds(elo0=1.0, elo1=1.0, lower_llr=-2.94, upper_llr=2.94)
        trinomial_draws_only = Outcomes((0, 600, 0), (0, 0, 300, 0, 0), False)
        cases = [
            (outcomes((0, 1, 2, 1, 0)), 0.1, BOUNDS, EtaReason.TOO_FEW_GAMES),
            (outcomes(), 3.0, BOUNDS, EtaReason.OUTSIDE_BOUNDS),
            (outcomes(), -2.94, BOUNDS, EtaReason.OUTSIDE_BOUNDS),
            (trinomial_draws_only, 0.0, BOUNDS, EtaReason.EMPTY_OUTCOME),
            (outcomes(), 0.0, flat, EtaReason.NO_VARIANCE),
        ]
        for results, llr, bounds, reason in cases:
            self.assertIsNone(forecast_sprt(results, llr, bounds), reason)
            self.assertEqual(sprt_unavailable_reason(results, llr, bounds), reason)

    def test_unavailable_sprt_eta_reports_its_reason(self):
        eta = estimate_eta(facts(llr=5.0), self.rate, at(0))
        self.assertEqual((eta.kind, eta.reason), (EtaKind.UNAVAILABLE, EtaReason.OUTSIDE_BOUNDS))


class SeriesTests(SimpleTestCase):
    def test_points_carry_elo_and_llr(self):
        point = ProgressPoint(at(1), outcomes().games, outcomes(), 1.5)
        series = build_series([point], with_llr=True, with_elo=True)
        self.assertEqual((series[0].llr, series[0].elo), (1.5, Elo(PENTA)[1]))
        self.assertEqual((series[0].elo_lower, series[0].elo_upper), (Elo(PENTA)[0], Elo(PENTA)[2]))

    def test_optional_columns(self):
        empty = ProgressPoint(at(1), 0, outcomes((0, 0, 0, 0, 0)), 0.0)
        series = build_series([empty], with_llr=False, with_elo=True)
        self.assertEqual((series[0].llr, series[0].elo, series[0].elo_lower), (None, None, None))


class ContributionTests(SimpleTestCase):
    def row(self, machine_id, cpu, penta, name=None, host=None):
        return ResultRow(machine_id, HostKey(host or f'host-{machine_id}'), name, 'owner', cpu, outcomes(penta))

    def test_machines_and_cpus(self):
        rows = [
            self.row(1, 'Ryzen', (1, 10, 20, 10, 1), 'fast'),
            self.row(2, 'Ryzen', (0, 5, 10, 5, 0)),
            self.row(3, None, (0, 1, 2, 1, 0)),
        ]
        summary = summarize_contributions(rows, use_penta=True, elapsed_seconds=7200)

        self.assertEqual([m.machine_id for m in summary.machines], [1, 2, 3])
        first = summary.machines[0]
        self.assertEqual((first.machine_name, first.stats.pairs, first.stats.games), ('fast', 42, 84))
        self.assertEqual(first.stats.pairs_per_hour, 21.0)
        self.assertAlmostEqual(sum(present(m.stats.share) for m in summary.machines), 1.0)
        self.assertEqual(summary.machines[2].cpu_name, 'Unknown')

        self.assertEqual(
            [(c.cpu_name, c.machines, c.stats.pairs) for c in summary.cpus], [('Ryzen', 2, 62), ('Unknown', 1, 4)]
        )

    def test_registrations_of_one_host_are_one_machine(self):
        first, second, third = (1, 10, 20, 10, 1), (0, 1, 2, 1, 0), (2, 3, 9, 6, 0)
        rows = [
            self.row(4, 'Ryzen', first, 'box', host='supervised'),
            self.row(9, 'Ryzen', second, 'box', host='supervised'),
            self.row(7, 'Ryzen', third, 'box', host='supervised'),
            self.row(5, 'Ryzen', (0, 1, 1, 1, 0), 'other'),
        ]
        summary = summarize_contributions(rows, use_penta=True, elapsed_seconds=3600)
        pooled = tuple(sum(counts) for counts in zip(first, second, third, strict=True))

        self.assertEqual([m.machine_name for m in summary.machines], ['box', 'other'])
        host = summary.machines[0]
        self.assertEqual((host.machine_id, host.stats.pairs, host.stats.games), (9, sum(pooled), 2 * sum(pooled)))
        self.assertEqual(
            [(r.machine_id, r.games, r.pairs) for r in host.registrations], [(9, 8, 4), (7, 40, 20), (4, 84, 42)]
        )
        self.assertEqual(sum(r.games for r in host.registrations), host.stats.games)
        elo = present(host.stats.elo)
        self.assertEqual((elo.lower, elo.value, elo.upper), Elo(pooled))
        self.assertEqual([(c.cpu_name, c.machines) for c in summary.cpus], [('Ryzen', 2)])

    def test_ephemeral_job_names_are_shortened_and_pooled(self):
        name = 'batch-a0741747-7d81-49a2-b96e-4b14d4c304c3:0'
        rows = [self.row(1, 'AMD EPYC 9R14', (0, 1, 2, 1, 0), name), self.row(2, 'AMD EPYC 9R14', (0, 1, 1, 1, 0))]
        job, unnamed = summarize_contributions(rows, use_penta=True, elapsed_seconds=None).machines
        self.assertEqual((job.machine_name, job.machine_label, job.pool), (name, 'batch-a0741747…:0', 'batch-*'))
        self.assertEqual((unnamed.machine_label, unnamed.pool), (None, 'AMD EPYC 9R14'))

    def test_empty_and_no_elapsed(self):
        self.assertEqual(summarize_contributions([], True, None).machines, [])
        summary = summarize_contributions([self.row(1, 'x', (0, 0, 0, 0, 0))], True, 0.0)
        self.assertEqual((summary.machines[0].stats.share, summary.machines[0].stats.pairs_per_hour), (None, None))

    def test_sum_by_key_uses_the_missing_key(self):
        rows = [('a', (1, 2)), (None, (3, 4)), ('', (5, 6)), ('a', (1, 1))]
        self.assertEqual(sum_by_key(rows, lambda r: r[0], lambda r: r[1], 'Unknown'), {'a': [2, 3], 'Unknown': [8, 10]})


class ServerTests(SimpleTestCase):
    def test_games_in_window_interpolates_the_start(self):
        since = at(60)
        before = {1: Edge(at(30), 100)}
        after = {1: Edge(at(90), 400), 2: Edge(at(70), 50)}
        self.assertEqual(games_in_window(since, before, after, {1: 1000, 2: 80}), (1000 - 250) + 80)

    def test_fleet_status(self):
        fleet = fleet_status([({'concurrency': 4}, 1.5), ({'concurrency': 2}, 1.0)])
        self.assertEqual((fleet.machines, fleet.threads, fleet.mnps), (2, 6, 8.0))

    def test_finished_summary(self):
        items = [
            FinishedWorkload(WorkloadMode.SPRT, True, False),
            FinishedWorkload(WorkloadMode.SPRT, False, True),
            FinishedWorkload(WorkloadMode.SPRT, True, False),
            FinishedWorkload(WorkloadMode.SPRT, False, False),
            FinishedWorkload(WorkloadMode.GAMES, True, False),
        ]
        summary = summarize_finished(items, timedelta(days=7))
        self.assertEqual((summary.total, summary.passed, summary.failed, summary.stopped), (5, 3, 1, 1))
        self.assertAlmostEqual(present(summary.sprt_pass_rate), 2 / 3)
        self.assertIsNone(summarize_finished([], timedelta(days=7)).sprt_pass_rate)

    def test_finished_summary_counts_completed_tunes_apart(self):
        items = [
            FinishedWorkload(WorkloadMode.SPSA, False, False, completed=True),
            FinishedWorkload(WorkloadMode.SPSA, False, False),
        ]
        summary = summarize_finished(items, timedelta(days=7))
        self.assertEqual((summary.total, summary.completed, summary.stopped), (2, 1, 1))


class InsightsAssemblyTests(SimpleTestCase):
    def test_legacy_workload_gets_a_synthetic_point(self):
        insights = build_insights(facts(), [], [], at(120))
        self.assertTrue(insights.history.synthetic)
        self.assertEqual(len(insights.history.points), 1)
        self.assertEqual(insights.history.points[0].games, outcomes().games)
        timing = present(insights.timing)
        self.assertEqual(timing.started_at, T0)
        self.assertGreater(present(timing.overall).games_per_hour, 0)

    def test_fresh_workload(self):
        insights = build_insights(facts(penta=(0, 0, 0, 0, 0), llr=0.0), [], [], at(10))
        self.assertFalse(insights.history.synthetic)
        self.assertEqual(insights.history.points, [])
        self.assertEqual(insights.eta.kind, EtaKind.UNAVAILABLE)
        self.assertIsNone(present(insights.strength).elo)

    def test_current_counters_extend_the_history(self):
        early = outcomes((1, 10, 20, 10, 1))
        insights = build_insights(facts(), [ProgressPoint(at(5), early.games, early, 0.1)], [], at(120))
        self.assertFalse(insights.history.synthetic)
        self.assertEqual([p.games for p in insights.history.points], [early.games, outcomes().games])

    def test_spsa_has_no_strength(self):
        insights = build_insights(facts(WorkloadMode.SPSA, target=100000), [], [], at(120))
        self.assertIsNone(insights.strength)
        self.assertIsNone(insights.history.points[0].elo)
        self.assertIsNone(insights.progress.llr)


class SerializeTests(SimpleTestCase):
    def test_json_types(self):
        payload = to_json(build_insights(facts(), [], [], at(120)))
        json.dumps(payload, allow_nan=False)
        self.assertEqual(field(payload, 'workload', 'mode'), 'SPRT')
        self.assertEqual(field(payload, 'workload', 'created_at'), '2026-09-01T00:00:00+00:00')
        self.assertEqual(field(payload, 'progress', 'pentanomial'), list(PENTA))

    def test_non_finite_floats_become_null(self):
        self.assertEqual(to_json([math.nan, math.inf, 1.5]), [None, None, 1.5])

    def test_rejects_unknown_types(self):
        with self.assertRaises(TypeError):
            to_json(object())
