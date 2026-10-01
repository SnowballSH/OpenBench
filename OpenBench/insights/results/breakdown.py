from dataclasses import dataclass

from OpenBench.insights.domain import Outcomes
from OpenBench.insights.strength import score_moments

GAMES_PER_PAIR = 2


@dataclass(frozen=True, slots=True)
class PairVariance:
    observed: float
    independent: float
    ratio: float
    game_correlation: float
    pair_efficiency: float


@dataclass(frozen=True, slots=True)
class OutcomeBreakdown:
    trinomial_fractions: tuple[float, ...] | None
    pentanomial_fractions: tuple[float, ...] | None
    decisive_game_rate: float | None
    games_per_decisive: float | None
    decisive_pair_rate: float | None
    level_pair_rate: float | None
    pair_variance: PairVariance | None


def fractions(counts: tuple[int, ...]) -> tuple[float, ...] | None:
    total = sum(counts)
    return tuple(n / total for n in counts) if total else None


def decisive_games(outcomes: Outcomes) -> int:
    losses, _, wins = outcomes.trinomial
    return losses + wins


def pair_variance(outcomes: Outcomes) -> PairVariance | None:

    if outcomes.games != GAMES_PER_PAIR * outcomes.pairs:
        return None

    pairs = score_moments(outcomes.pentanomial)
    games = score_moments(outcomes.trinomial)
    if not pairs or not games or not pairs.variance or not games.variance:
        return None

    # The mean of two independent games has half the variance of one game
    independent = games.variance / GAMES_PER_PAIR
    ratio = pairs.variance / independent
    return PairVariance(
        observed=pairs.variance,
        independent=independent,
        ratio=ratio,
        game_correlation=ratio - 1.0,
        pair_efficiency=1.0 / ratio,
    )


def outcome_breakdown(outcomes: Outcomes) -> OutcomeBreakdown:
    LL, _, DD, _, WW = outcomes.pentanomial
    decisive = decisive_games(outcomes)
    return OutcomeBreakdown(
        trinomial_fractions=fractions(outcomes.trinomial),
        pentanomial_fractions=fractions(outcomes.pentanomial),
        decisive_game_rate=decisive / outcomes.games if outcomes.games else None,
        games_per_decisive=outcomes.games / decisive if decisive else None,
        decisive_pair_rate=(LL + WW) / outcomes.pairs if outcomes.pairs else None,
        level_pair_rate=DD / outcomes.pairs if outcomes.pairs else None,
        pair_variance=pair_variance(outcomes),
    )
