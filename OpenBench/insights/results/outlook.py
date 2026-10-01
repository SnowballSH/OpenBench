import math
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray
from scipy.special import log_ndtr, ndtr

from OpenBench.insights.domain import Outcomes, SprtBounds
from OpenBench.insights.sprt import MIN_GAMES, MIN_VARIANCE, LlrIncrement, expected_exit, llr_increment

type Vector = NDArray[np.float64]

PRIOR = 'flat'
INTERVAL = 0.8
POSTERIOR_SPAN = 6.0
POSTERIOR_NODES = 121
SERIES_TERMS = 240
SERIES_DRIFT_LIMIT = 12.0
BISECTION_STEPS = 60
HORIZON_DOUBLINGS = 80


@dataclass(frozen=True, slots=True)
class GamesRange:
    lower: int
    median: int
    upper: int


@dataclass(frozen=True, slots=True)
class SprtOutlook:
    pass_probability: float
    remaining_games: GamesRange
    interval: float
    prior: str


@dataclass(frozen=True, slots=True)
class Walk:
    variance: float
    start: float
    lower: float
    upper: float

    @property
    def width(self) -> float:
        return self.upper - self.lower

    @property
    def offset(self) -> float:
        return self.start - self.lower

    @property
    def timescale(self) -> float:
        return self.width**2 / self.variance


@dataclass(frozen=True, slots=True)
class DriftPosterior:
    drifts: Vector
    weights: Vector


def steps_observed(outcomes: Outcomes) -> int:
    return outcomes.pairs if outcomes.use_penta else outcomes.games


def drift_posterior(increment: LlrIncrement, steps: int) -> DriftPosterior:

    # A flat prior on the drift and a Brownian LLR observed after `steps` steps give a normal posterior
    z = np.linspace(-POSTERIOR_SPAN, POSTERIOR_SPAN, POSTERIOR_NODES)
    density = np.exp(-0.5 * z**2)
    return DriftPosterior(
        drifts=increment.drift + z * math.sqrt(increment.variance / steps),
        weights=density / density.sum(),
    )


def pass_probability(posterior: DriftPosterior, walk: Walk) -> float:
    exits = (
        expected_exit(LlrIncrement(float(drift), walk.variance, 1), walk.start, walk.lower, walk.upper)
        for drift in posterior.drifts
    )
    return sum(
        float(weight) * exit.pass_probability for weight, exit in zip(posterior.weights, exits, strict=True) if exit
    )


class ExitTimeLaw:
    """Survival function of the exit time, averaged over the posterior of the drift."""

    def __init__(self, posterior: DriftPosterior, walk: Walk) -> None:
        tilt = posterior.drifts / walk.variance
        strong = np.abs(tilt) * walk.width > SERIES_DRIFT_LIMIT
        self.walk = walk
        self.series_coefficients, self.series_rates = series_terms(tilt[~strong], posterior.weights[~strong], walk)
        self.strong_drifts = np.abs(posterior.drifts[strong])
        self.strong_weights = posterior.weights[strong]
        self.strong_distances = np.where(posterior.drifts[strong] > 0, walk.upper - walk.start, walk.start - walk.lower)

    def survival(self, steps: float) -> float:
        series = float(np.sum(self.series_coefficients * np.exp(-self.series_rates * steps)))
        return min(1.0, max(0.0, series + self.one_sided_survival(steps)))

    def one_sided_survival(self, steps: float) -> float:

        # With a strong drift only the bound it points at matters: the inverse Gaussian first-passage law
        if not self.strong_drifts.size:
            return 0.0
        if steps <= 0.0:
            return float(np.sum(self.strong_weights))

        spread = math.sqrt(self.walk.variance * steps)
        travelled = self.strong_drifts * steps
        reached = ndtr((travelled - self.strong_distances) / spread)
        reflected = np.exp(
            2 * self.strong_drifts * self.strong_distances / self.walk.variance
            + log_ndtr(-(travelled + self.strong_distances) / spread)
        )
        return float(np.sum(self.strong_weights * (1.0 - np.minimum(1.0, reached + reflected))))

    def quantile(self, probability: float) -> float:

        low = high = self.walk.timescale / SERIES_TERMS**2
        for _ in range(HORIZON_DOUBLINGS):
            if self.survival(high) <= 1.0 - probability:
                break
            low, high = high, 2 * high

        for _ in range(BISECTION_STEPS):
            middle = math.sqrt(low * high)
            low, high = (middle, high) if self.survival(middle) > 1.0 - probability else (low, middle)

        return high


def series_terms(tilt: Vector, weights: Vector, walk: Walk) -> tuple[Vector, Vector]:

    # Eigenfunction expansion of Brownian motion with drift, killed at both bounds, integrated over the interval
    n = np.arange(1, SERIES_TERMS + 1)[None, :]
    frequency = n * math.pi / walk.width
    tilt = tilt[:, None]

    edges = np.exp(-tilt * walk.offset) - np.where(n % 2, -1.0, 1.0) * np.exp(tilt * (walk.width - walk.offset))
    shape = 2 * frequency / walk.width * np.sin(frequency * walk.offset) / (tilt**2 + frequency**2)
    rates = 0.5 * walk.variance * (tilt**2 + frequency**2)
    return weights[:, None] * shape * edges, rates


def games_range(law: ExitTimeLaw, games_per_step: int) -> GamesRange:
    tail = (1.0 - INTERVAL) / 2
    lower, median, upper = (
        math.ceil(law.quantile(probability)) * games_per_step for probability in (tail, 0.5, 1.0 - tail)
    )
    return GamesRange(lower, median, upper)


def sprt_outlook(outcomes: Outcomes, llr: float, bounds: SprtBounds) -> SprtOutlook | None:

    if outcomes.games < MIN_GAMES or not bounds.lower_llr < llr < bounds.upper_llr:
        return None

    if not (increment := llr_increment(outcomes, bounds)) or increment.variance < MIN_VARIANCE:
        return None

    walk = Walk(increment.variance, llr, bounds.lower_llr, bounds.upper_llr)
    posterior = drift_posterior(increment, steps_observed(outcomes))

    return SprtOutlook(
        pass_probability=pass_probability(posterior, walk),
        remaining_games=games_range(ExitTimeLaw(posterior, walk), increment.games_per_step),
        interval=INTERVAL,
        prior=PRIOR,
    )
