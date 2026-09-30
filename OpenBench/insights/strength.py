import math
from collections.abc import Sequence
from dataclasses import dataclass
from statistics import NormalDist

import OpenBench.stats
from OpenBench.insights.domain import Outcomes

NORMAL = NormalDist()
Z_95 = NORMAL.inv_cdf(0.975)
NELO_PER_T = 800 / math.log(10)


@dataclass(frozen=True, slots=True)
class EloInterval:
    lower: float
    value: float
    upper: float


@dataclass(frozen=True, slots=True)
class ScoreMoments:
    mean: float
    variance: float
    count: int


@dataclass(frozen=True, slots=True)
class StrengthSummary:
    elo: EloInterval | None
    normalized_elo: EloInterval | None
    los: float | None
    draw_ratio: float | None
    penta_fractions: tuple[float, ...] | None


def score_moments(results: Sequence[int]) -> ScoreMoments | None:

    if (count := sum(results)) < 2:
        return None

    div = len(results) - 1
    mean = sum((i / div) * n for i, n in enumerate(results)) / count
    var = sum(((i / div) - mean) ** 2 * n for i, n in enumerate(results)) / count
    return ScoreMoments(mean, var, count)


def elo_interval(results: Sequence[int]) -> EloInterval | None:

    if sum(results) < 2:
        return None

    lower, value, upper = OpenBench.stats.Elo(tuple(results))
    return EloInterval(lower, value, upper)


def likelihood_of_superiority(results: Sequence[int]) -> float | None:

    if not (moments := score_moments(results)):
        return None

    if moments.variance == 0.0:
        return 0.5 if moments.mean == 0.5 else float(moments.mean > 0.5)

    standard_error = math.sqrt(moments.variance / moments.count)
    return NORMAL.cdf((moments.mean - 0.5) / standard_error)


def normalized_elo(results: Sequence[int]) -> EloInterval | None:

    # https://hardy.uhasselt.be/Fishtest/normalized_elo_practical.pdf, as in OpenBench.stats.PentanomialSPRT
    if not (moments := score_moments(results)) or moments.variance == 0.0:
        return None

    per_t = NELO_PER_T / math.sqrt(2) if len(results) == 5 else NELO_PER_T
    value = per_t * (moments.mean - 0.5) / math.sqrt(moments.variance)
    margin = per_t * Z_95 / math.sqrt(moments.count)
    return EloInterval(value - margin, value, value + margin)


def draw_ratio(outcomes: Outcomes) -> float | None:
    return outcomes.draws / outcomes.games if outcomes.games else None


def penta_fractions(outcomes: Outcomes) -> tuple[float, ...] | None:
    return tuple(n / outcomes.pairs for n in outcomes.pentanomial) if outcomes.pairs else None


def summarize_strength(outcomes: Outcomes) -> StrengthSummary:
    results = outcomes.primary()
    return StrengthSummary(
        elo=elo_interval(results),
        normalized_elo=normalized_elo(results),
        los=likelihood_of_superiority(results),
        draw_ratio=draw_ratio(outcomes),
        penta_fractions=penta_fractions(outcomes),
    )
