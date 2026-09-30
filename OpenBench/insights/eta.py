from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from OpenBench.insights.domain import Outcomes, SprtBounds, WorkloadFacts, WorkloadMode
from OpenBench.insights.sprt import MIN_GAMES, forecast_sprt, llr_increment
from OpenBench.insights.timing import Rate


class EtaKind(StrEnum):
    FINISHED = 'finished'
    TARGET = 'target'
    SPRT = 'sprt_estimate'
    UNAVAILABLE = 'unavailable'


class EtaReason(StrEnum):
    TOO_FEW_GAMES = 'too_few_games'
    OUTSIDE_BOUNDS = 'outside_bounds'
    EMPTY_OUTCOME = 'empty_outcome'
    NO_VARIANCE = 'no_variance'
    NO_TARGET = 'no_target'
    NO_RATE = 'no_rate'


@dataclass(frozen=True, slots=True)
class Eta:
    kind: EtaKind
    remaining_games: int | None
    remaining_seconds: float | None
    completes_at: datetime | None
    reason: EtaReason | None = None


def remaining_target_games(facts: WorkloadFacts) -> int | None:
    if facts.target_games is None:
        return None
    return max(0, facts.target_games - facts.outcomes.games)


def seconds_for(games: int, rate: Rate | None) -> float | None:
    if rate is None or rate.games_per_hour <= 0:
        return None
    return 3600 * games / rate.games_per_hour


def completion_time(now: datetime, seconds: float | None) -> datetime | None:
    if seconds is None:
        return None
    try:
        return now + timedelta(seconds=seconds)
    except OverflowError:
        return None


def with_rate(kind: EtaKind, games: int, rate: Rate | None, now: datetime) -> Eta:
    seconds = seconds_for(games, rate)
    return Eta(
        kind=kind,
        remaining_games=games,
        remaining_seconds=seconds,
        completes_at=completion_time(now, seconds),
        reason=EtaReason.NO_RATE if seconds is None else None,
    )


def sprt_unavailable_reason(outcomes: Outcomes, llr: float, bounds: SprtBounds) -> EtaReason:
    if outcomes.games < MIN_GAMES:
        return EtaReason.TOO_FEW_GAMES
    if not bounds.lower_llr < llr < bounds.upper_llr:
        return EtaReason.OUTSIDE_BOUNDS
    if llr_increment(outcomes, bounds) is None:
        return EtaReason.EMPTY_OUTCOME
    return EtaReason.NO_VARIANCE


def unavailable(reason: EtaReason) -> Eta:
    return Eta(EtaKind.UNAVAILABLE, None, None, None, reason)


def estimate_eta(facts: WorkloadFacts, rate: Rate | None, now: datetime) -> Eta:

    if facts.finished:
        return Eta(EtaKind.FINISHED, 0, 0.0, None)

    if facts.mode == WorkloadMode.SPRT and facts.sprt is not None:
        if (forecast := forecast_sprt(facts.outcomes, facts.llr, facts.sprt)) is None:
            return unavailable(sprt_unavailable_reason(facts.outcomes, facts.llr, facts.sprt))
        return with_rate(EtaKind.SPRT, forecast.remaining_games, rate, now)

    if (remaining := remaining_target_games(facts)) is not None:
        return with_rate(EtaKind.TARGET, remaining, rate, now)

    return unavailable(EtaReason.NO_TARGET)
