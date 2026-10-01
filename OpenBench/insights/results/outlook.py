import math
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray
from scipy.special import log_ndtr

from OpenBench.insights.domain import Outcomes, SprtBounds
from OpenBench.insights.sprt import MIN_GAMES, MIN_VARIANCE, LlrIncrement, expected_exit, llr_increment
from OpenBench.insights.strength import NELO_PER_T, score_moments

type Vector = NDArray[np.float64]

INTERVAL = 0.8
PRIOR_SD_FLOOR = 4.0
PRIOR_SD_PER_BOUND_WIDTH = 4.0 / 3.0
MAX_ERROR_PER_BOUND_WIDTH = 2.0
POSTERIOR_SPAN = 6.0
POSTERIOR_NODES = 61
SERIES_TERMS = 24
SERIES_DRIFT_LIMIT = 8.0
IMAGE_REFLECTIONS = 2
IMAGE_SPREAD_LIMIT = 0.5
MAX_EXPONENT = 700.0
BISECTION_STEPS = 24
HORIZON_DOUBLINGS = 80


@dataclass(frozen=True, slots=True)
class GamesRange:
    lower: int
    median: int
    upper: int


@dataclass(frozen=True, slots=True)
class Prior:
    mean_elo: float
    sd_elo: float
    equivalent_games: int


@dataclass(frozen=True, slots=True)
class SprtOutlook:
    pass_probability: float
    remaining_games: GamesRange
    interval: float
    prior: Prior


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


@dataclass(frozen=True, slots=True)
class Evidence:
    """What the games say about the normalized Elo, in t-value units per step of the SPRT."""

    increment: LlrIncrement
    steps: int
    t_value: float
    elo_per_t: float

    @property
    def elo_error(self) -> float:
        return self.elo_per_t / math.sqrt(self.steps)


def gather_evidence(outcomes: Outcomes, bounds: SprtBounds) -> Evidence | None:

    moments = score_moments(outcomes.primary())
    increment = llr_increment(outcomes, bounds)
    if not moments or not moments.variance or not increment or increment.variance < MIN_VARIANCE:
        return None

    return Evidence(
        increment=increment,
        steps=moments.count,
        t_value=(moments.mean - 0.5) / math.sqrt(moments.variance),
        elo_per_t=NELO_PER_T / math.sqrt(2) if outcomes.use_penta else NELO_PER_T,
    )


def prior_sd(bounds: SprtBounds) -> float:
    return max(PRIOR_SD_FLOOR, PRIOR_SD_PER_BOUND_WIDTH * (bounds.elo1 - bounds.elo0))


def precise_enough(evidence: Evidence, bounds: SprtBounds) -> bool:
    return evidence.elo_error <= MAX_ERROR_PER_BOUND_WIDTH * (bounds.elo1 - bounds.elo0)


def drift_posterior(evidence: Evidence, prior_sd_elo: float) -> DriftPosterior:

    # The LLR drifts by sigma per unit of t-value, so a normal prior on the Elo is one on the drift:
    # it acts like `prior_steps` earlier steps that averaged exactly zero Elo
    sigma = math.sqrt(evidence.increment.variance)
    prior_steps = (evidence.elo_per_t / prior_sd_elo) ** 2
    drift_at_zero_elo = evidence.increment.drift - sigma * evidence.t_value
    steps = evidence.steps + prior_steps
    mean = (evidence.steps * evidence.increment.drift + prior_steps * drift_at_zero_elo) / steps

    z = np.linspace(-POSTERIOR_SPAN, POSTERIOR_SPAN, POSTERIOR_NODES)
    density = np.exp(-0.5 * z**2)
    return DriftPosterior(drifts=mean + z * sigma / math.sqrt(steps), weights=density / density.sum())


def pass_probability(posterior: DriftPosterior, walk: Walk) -> float:
    exits = (
        expected_exit(LlrIncrement(float(drift), walk.variance, 1), walk.start, walk.lower, walk.upper)
        for drift in posterior.drifts
    )
    return sum(
        float(weight) * exit.pass_probability for weight, exit in zip(posterior.weights, exits, strict=True) if exit
    )


def series_terms(tilt: Vector, weights: Vector, walk: Walk) -> tuple[Vector, Vector]:

    # Eigenfunction expansion of Brownian motion with drift, killed at both bounds, integrated over the interval
    n = np.arange(1, SERIES_TERMS + 1)[None, :]
    frequency = n * math.pi / walk.width
    tilt = tilt[:, None]

    edges = np.exp(-tilt * walk.offset) - np.where(n % 2, -1.0, 1.0) * np.exp(tilt * (walk.width - walk.offset))
    shape = 2 * frequency / walk.width * np.sin(frequency * walk.offset) / (tilt**2 + frequency**2)
    rates = 0.5 * walk.variance * (tilt**2 + frequency**2)
    return weights[:, None] * shape * edges, rates


def tilted_mass(shift: Vector, upper: Vector, lower: Vector) -> Vector:

    # exp(shift) * (Phi(upper) - Phi(lower)) without overflow, taking the difference in whichever tail is small
    flip = lower > 0
    high = np.where(flip, -lower, upper)
    low = np.where(flip, -upper, lower)
    log_high = log_ndtr(high)
    log_mass = log_high + np.log1p(-np.exp(np.minimum(0.0, log_ndtr(low) - log_high)))
    return np.asarray(np.exp(np.minimum(MAX_EXPONENT, shift + log_mass)), dtype=np.float64)


class ExitTimeLaw:
    """Survival function of the exit time, averaged over the posterior of the drift."""

    def __init__(self, posterior: DriftPosterior, walk: Walk) -> None:
        tilt = posterior.drifts / walk.variance
        strong = np.abs(tilt) * walk.width > SERIES_DRIFT_LIMIT
        self.walk = walk
        self.posterior = posterior
        self.strong = DriftPosterior(posterior.drifts[strong], posterior.weights[strong])
        self.series_coefficients, self.series_rates = series_terms(tilt[~strong], posterior.weights[~strong], walk)
        self.reflections: Vector = 2.0 * walk.width * np.arange(-IMAGE_REFLECTIONS, IMAGE_REFLECTIONS + 1.0)[None, :]

    def survival(self, steps: float) -> float:

        # The images converge while the walk has spread less than the bounds are apart, the series once it
        # has spread further; a strong drift ends the test before that, and makes the series cancel badly
        if steps <= 0.0:
            return 1.0
        if math.sqrt(self.walk.variance * steps) < IMAGE_SPREAD_LIMIT * self.walk.width:
            return self.image_survival(self.posterior, steps)

        series = float(np.sum(self.series_coefficients * np.exp(-self.series_rates * steps)))
        return min(1.0, max(0.0, series + self.image_survival(self.strong, steps)))

    def image_survival(self, posterior: DriftPosterior, steps: float) -> float:

        # Method of images: the walk and its reflections in both bounds, each weighted for the drift
        if not posterior.weights.size:
            return 0.0

        walk = self.walk
        spread = math.sqrt(walk.variance * steps)
        drifts = posterior.drifts[:, None]
        tilt = drifts / walk.variance
        travelled = drifts * steps

        def mass(source: Vector) -> Vector:
            return tilted_mass(
                tilt * (source - walk.offset),
                (walk.width - source - travelled) / spread,
                (-source - travelled) / spread,
            )

        inside = np.subtract(mass(self.reflections + walk.offset), mass(self.reflections - walk.offset))
        return float(np.sum(posterior.weights * np.clip(inside.sum(axis=1), 0.0, 1.0)))

    def quantile(self, probability: float, at_least: float = 1.0) -> float:

        low = high = at_least
        for _ in range(HORIZON_DOUBLINGS):
            if self.survival(high) <= 1.0 - probability:
                break
            low, high = high, 2 * high

        for _ in range(BISECTION_STEPS):
            middle = math.sqrt(low * high)
            low, high = (middle, high) if self.survival(middle) > 1.0 - probability else (low, middle)

        return high


def games_range(law: ExitTimeLaw, games_per_step: int) -> GamesRange:
    tail = (1.0 - INTERVAL) / 2
    lower = law.quantile(tail)
    median = law.quantile(0.5, at_least=lower)
    upper = law.quantile(1.0 - tail, at_least=median)
    return GamesRange(*(math.ceil(steps) * games_per_step for steps in (lower, median, upper)))


def forecast(evidence: Evidence, llr: float, bounds: SprtBounds, prior_sd_elo: float) -> SprtOutlook:
    increment = evidence.increment
    walk = Walk(increment.variance, llr, bounds.lower_llr, bounds.upper_llr)
    posterior = drift_posterior(evidence, prior_sd_elo)
    prior_steps = (evidence.elo_per_t / prior_sd_elo) ** 2

    return SprtOutlook(
        pass_probability=pass_probability(posterior, walk),
        remaining_games=games_range(ExitTimeLaw(posterior, walk), increment.games_per_step),
        interval=INTERVAL,
        prior=Prior(0.0, prior_sd_elo, round(prior_steps) * increment.games_per_step),
    )


def sprt_outlook(outcomes: Outcomes, llr: float, bounds: SprtBounds) -> SprtOutlook | None:

    if outcomes.games < MIN_GAMES or not bounds.lower_llr < llr < bounds.upper_llr:
        return None

    evidence = gather_evidence(outcomes, bounds)
    if evidence is None or not precise_enough(evidence, bounds):
        return None

    return forecast(evidence, llr, bounds, prior_sd(bounds))
