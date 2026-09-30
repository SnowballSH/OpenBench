import math

import numpy as np
from django.test import SimpleTestCase
from scipy import optimize

from OpenBench.stats import (
    PENTANOMIAL_NELO_LIMIT,
    Elo,
    MLE_tvalue,
    PentanomialSPRT,
    TrinomialSPRT,
    bayeselo_to_proba,
    proba_to_bayeselo,
)

R3 = (22569, 44137, 22976)
R5 = (39, 8843, 26675, 9240, 44)

DEGENERATE_PENTA = [(0, 0, 0, 0, 0), (100, 0, 0, 0, 0), (0, 0, 100, 0, 0), (0, 0, 0, 0, 100), (1, 0, 0, 0, 1)]


def constrained_mle(pdfhat: np.ndarray, t: float) -> np.ndarray:

    # The distribution on the five pair outcomes that maximises the likelihood
    # of pdfhat, subject to having t-value t, found by a generic optimiser
    points = np.linspace(0, 1, 5)

    def t_value(q):
        mean = q @ points
        return (mean - 0.5) / math.sqrt(q @ (points - mean) ** 2)

    solution = optimize.minimize(
        lambda q: -(pdfhat @ np.log(q)),
        np.full(5, 0.2),
        method='SLSQP',
        bounds=[(1e-12, 1)] * 5,
        constraints=[{'type': 'eq', 'fun': lambda q: q.sum() - 1}, {'type': 'eq', 'fun': lambda q: t_value(q) - t}],
        options={'ftol': 1e-15, 'maxiter': 1000},
    )
    return solution.x


def reference_penta_llr(results, elo0: float, elo1: float) -> float:
    counts = np.maximum(1e-3, np.array(results, dtype=float))
    pdfhat = counts / counts.sum()
    t0, t1 = (elo / (800 / math.log(10)) * math.sqrt(2) for elo in (elo0, elo1))
    return counts.sum() * (pdfhat @ (np.log(constrained_mle(pdfhat, t1)) - np.log(constrained_mle(pdfhat, t0))))


class PentanomialTests(SimpleTestCase):
    def test_matches_an_independent_constrained_mle(self):
        for results, bounds in [
            (R5, (0.5, 2.5)),
            ((5, 20, 50, 20, 5), (0, 5)),
            ((300, 1200, 2000, 1500, 400), (-3, 1)),
        ]:
            self.assertAlmostEqual(PentanomialSPRT(results, *bounds), reference_penta_llr(results, *bounds), places=6)

    def test_known_value(self):
        self.assertAlmostEqual(PentanomialSPRT(R5, 0.5, 2.5), 2.94168, places=4)

    def test_mle_has_the_requested_t_value(self):
        pdfhat = [(i / 4, p) for i, p in enumerate(np.array(R5) / sum(R5))]
        for t in (-0.5, 0.0, 0.1, 0.5):
            pdf = MLE_tvalue(pdfhat, 0.5, t)
            mu = sum(a * p for a, p in pdf)
            var = sum(p * (a - mu) ** 2 for a, p in pdf)
            self.assertAlmostEqual((mu - 0.5) / math.sqrt(var), t, places=5)

    def test_mirroring_the_results_mirrors_the_hypotheses(self):
        self.assertAlmostEqual(PentanomialSPRT(R5[::-1], -0.5, -2.5), PentanomialSPRT(R5, 0.5, 2.5), places=9)

    def test_sign_follows_the_results(self):
        self.assertGreater(PentanomialSPRT((10, 100, 300, 200, 40), 0, 5), 0)
        self.assertLess(PentanomialSPRT((40, 200, 300, 100, 10), 0, 5), 0)

    def test_more_wins_never_lower_the_llr(self):
        llrs = [PentanomialSPRT((50, 200, 300, 200 + k, 50 + k), 0, 5) for k in range(0, 200, 20)]
        self.assertEqual(llrs, sorted(llrs))

    def test_equal_hypotheses_give_zero(self):
        self.assertAlmostEqual(PentanomialSPRT(R5, 2.0, 2.0), 0.0, places=12)

    def test_degenerate_results_do_not_raise(self):
        for results in DEGENERATE_PENTA:
            for bounds in [(0, 5), (-5, 0), (-3, 1), (0, 20)]:
                self.assertTrue(math.isfinite(PentanomialSPRT(results, *bounds)), (results, bounds))

    def test_bounds_up_to_the_limit_are_computable(self):
        limit = PENTANOMIAL_NELO_LIMIT
        for results in DEGENERATE_PENTA + [R5]:
            for bounds in [(-limit, limit), (0, limit), (-limit, 0)]:
                self.assertTrue(math.isfinite(PentanomialSPRT(results, *bounds)), (results, bounds))

    def test_the_limit_sits_just_below_the_mathematical_edge(self):
        edge = (2 / 3) * 800 / math.log(10)
        self.assertLess(PENTANOMIAL_NELO_LIMIT, edge)
        with self.assertRaises(AssertionError):
            PentanomialSPRT(R5, 0, edge + 1e-6)


class TrinomialTests(SimpleTestCase):
    def test_known_value(self):
        self.assertAlmostEqual(TrinomialSPRT(R3, 0.5, 2.5), 0.975772, places=5)

    def test_requires_every_outcome(self):
        for results in [(0, 0, 0), (10, 0, 0), (0, 10, 0), (0, 0, 10), (0, 10, 10)]:
            self.assertEqual(TrinomialSPRT(results, 0, 5), 0.0)

    def test_mirroring_the_results_mirrors_the_hypotheses(self):
        self.assertAlmostEqual(TrinomialSPRT(R3[::-1], -0.5, -2.5), TrinomialSPRT(R3, 0.5, 2.5), places=6)

    def test_sign_follows_the_results(self):
        self.assertGreater(TrinomialSPRT((100, 300, 200), 0, 5), 0)
        self.assertLess(TrinomialSPRT((200, 300, 100), 0, 5), 0)

    def test_bayeselo_round_trip(self):
        for elo, draw_elo in [(0, 200), (15, 250), (-40, 120)]:
            recovered = proba_to_bayeselo(*bayeselo_to_proba(elo, draw_elo))
            self.assertAlmostEqual(recovered[0], elo, places=9)
            self.assertAlmostEqual(recovered[1], draw_elo, places=9)


class EloTests(SimpleTestCase):
    def test_no_games(self):
        self.assertEqual(Elo((0, 0, 0)), (0.0, 0.0, 0.0))
        self.assertEqual(Elo((0, 0, 0, 0, 0)), (0.0, 0.0, 0.0))
        self.assertEqual(Elo((0, 0, 1)), (0.0, 0.0, 0.0))

    def test_point_estimate(self):
        self.assertAlmostEqual(Elo((0, 1, 1))[1], 400 * math.log10(3), places=9)
        self.assertAlmostEqual(Elo((1, 2, 3, 2, 1))[1], 0.0, places=9)

    def test_even_results_are_not_negative_zero(self):
        self.assertEqual('%.2f' % Elo((1, 2, 3, 2, 1))[1], '0.00')
        self.assertEqual('%.2f' % Elo((5, 10, 5))[1], '0.00')

    def test_interval_uses_the_t_distribution(self):
        half = 2.0930240544083087 * 0.5 / math.sqrt(20)
        upper = -400 * math.log10(1 / (0.5 + half) - 1)
        self.assertAlmostEqual(Elo((10, 0, 10))[2], upper, places=9)

    def test_trinomial_and_pentanomial_agree_on_the_point_estimate(self):
        self.assertAlmostEqual(Elo((0, 10, 0, 30, 0))[1], Elo((10, 40, 30))[1], places=9)

    def test_swapping_wins_and_losses_negates(self):
        for results in [R3, R5, (3, 7, 11), (1, 4, 10, 6, 2)]:
            lower, elo, upper = Elo(results)
            mirror = Elo(results[::-1])
            for a, b in zip((lower, elo, upper), (-mirror[2], -mirror[1], -mirror[0])):
                self.assertAlmostEqual(a, b, places=9)

    def test_ordering(self):
        for results in [R3, R5, (3, 7, 11), (1, 4, 10, 6, 2)]:
            lower, elo, upper = Elo(results)
            self.assertLessEqual(lower, elo)
            self.assertLessEqual(elo, upper)

    def test_extremes_are_finite(self):
        for results in [(0, 0, 50), (50, 0, 0), (0, 50, 0), (0, 0, 0, 0, 50), (50, 0, 0, 0, 0), (0, 0, 50, 0, 0)]:
            self.assertTrue(all(math.isfinite(x) for x in Elo(results)), results)
