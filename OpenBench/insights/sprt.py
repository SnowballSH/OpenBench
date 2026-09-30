import math
from collections.abc import Sequence
from dataclasses import dataclass

import OpenBench.stats

from OpenBench.insights.domain import Outcomes, SprtBounds

MIN_GAMES         = 200
MIN_VARIANCE      = 1e-12
LINEAR_DRIFT_EPS  = 1e-9
PENTA_COUNT_FLOOR = 1e-3

@dataclass(frozen=True, slots=True)
class LlrIncrement:
    drift          : float
    variance       : float
    games_per_step : int

@dataclass(frozen=True, slots=True)
class ExitEstimate:
    steps            : float
    pass_probability : float

@dataclass(frozen=True, slots=True)
class SprtForecast:
    remaining_games : int
    drift_per_game  : float

def weighted_moments(weights: Sequence[float], values: Sequence[float]) -> tuple[float, float]:
    mean = sum(w * v for w, v in zip(weights, values))
    return mean, sum(w * (v - mean) ** 2 for w, v in zip(weights, values))

def pentanomial_increment(penta: Sequence[int], elo0: float, elo1: float) -> LlrIncrement:

    # Per-pair log-likelihood ratio under the same MLE laws as OpenBench.stats.PentanomialSPRT
    counts = [max(PENTA_COUNT_FLOOR, n) for n in penta]
    total  = sum(counts)
    pdf    = [(i / 4, n / total) for i, n in enumerate(counts)]

    t0, t1     = (math.sqrt(2) * elo * math.log(10) / 800 for elo in (elo0, elo1))
    pdf0, pdf1 = (OpenBench.stats.MLE_tvalue(pdf, 0.5, t) for t in (t0, t1))
    increments = [math.log(pdf1[i][1]) - math.log(pdf0[i][1]) for i in range(len(pdf))]

    drift, variance = weighted_moments([p for _, p in pdf], increments)
    return LlrIncrement(drift, variance, games_per_step=2)

def trinomial_increment(tri: Sequence[int], elo0: float, elo1: float) -> LlrIncrement | None:

    # Per-game log-likelihood ratio under the BayesElo laws of OpenBench.stats.TrinomialSPRT
    if not all(tri):
        return None

    total      = sum(tri)
    pdf        = [n / total for n in tri]
    _, drawelo = OpenBench.stats.proba_to_bayeselo(*pdf)
    pdf0, pdf1 = (OpenBench.stats.bayeselo_to_proba(elo, drawelo) for elo in (elo0, elo1))
    increments = [math.log(pdf1[i] / pdf0[i]) for i in range(3)]

    drift, variance = weighted_moments(pdf, increments)
    return LlrIncrement(drift, variance, games_per_step=1)

def upper_exit_probability(k: float, start: float, lower: float, upper: float) -> float:

    # P(hit upper before lower) for Brownian motion with 2 * drift / variance = k, rearranged to avoid overflow
    if k > 0:
        return math.expm1(-k * (start - lower)) / math.expm1(-k * (upper - lower))
    return (math.exp(k * (upper - lower)) - math.exp(k * (upper - start))) / math.expm1(k * (upper - lower))

def expected_exit(increment: LlrIncrement, start: float, lower: float, upper: float) -> ExitEstimate | None:

    if not lower < start < upper or increment.variance < MIN_VARIANCE:
        return None

    k = 2 * increment.drift / increment.variance

    if abs(k * (upper - lower)) < LINEAR_DRIFT_EPS:
        probability = (start - lower) / (upper - lower)
        steps       = (start - lower) * (upper - start) / increment.variance
    else:
        probability = upper_exit_probability(k, start, lower, upper)
        steps       = (probability * (upper - lower) - (start - lower)) / increment.drift

    return ExitEstimate(steps=max(0.0, steps), pass_probability=min(1.0, max(0.0, probability)))

def llr_increment(outcomes: Outcomes, bounds: SprtBounds) -> LlrIncrement | None:
    if outcomes.use_penta:
        return pentanomial_increment(outcomes.pentanomial, bounds.elo0, bounds.elo1)
    return trinomial_increment(outcomes.trinomial, bounds.elo0, bounds.elo1)

def forecast_sprt(outcomes: Outcomes, llr: float, bounds: SprtBounds) -> SprtForecast | None:

    if outcomes.games < MIN_GAMES:
        return None

    if not (increment := llr_increment(outcomes, bounds)):
        return None

    if not (estimate := expected_exit(increment, llr, bounds.lower_llr, bounds.upper_llr)):
        return None

    return SprtForecast(
        remaining_games = math.ceil(estimate.steps) * increment.games_per_step,
        drift_per_game  = increment.drift / increment.games_per_step,
    )
