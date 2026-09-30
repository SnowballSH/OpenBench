from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from OpenBench.insights.domain import WorkloadFacts, WorkloadMode
from OpenBench.insights.sprt import forecast_sprt
from OpenBench.insights.timing import Rate

class EtaKind(StrEnum):
    FINISHED    = 'finished'
    TARGET      = 'target'
    SPRT        = 'sprt_estimate'
    UNAVAILABLE = 'unavailable'

@dataclass(frozen=True, slots=True)
class Eta:
    kind              : EtaKind
    remaining_games   : int | None
    remaining_seconds : float | None
    completes_at      : datetime | None

def remaining_target_games(facts: WorkloadFacts) -> int | None:
    if facts.target_games is None:
        return None
    return max(0, facts.target_games - facts.outcomes.games)

def seconds_for(games: int, rate: Rate | None) -> float | None:
    if rate is None or rate.games_per_hour <= 0:
        return None
    return 3600 * games / rate.games_per_hour

def with_rate(kind: EtaKind, games: int, rate: Rate | None, now: datetime) -> Eta:
    seconds = seconds_for(games, rate)
    return Eta(
        kind              = kind,
        remaining_games   = games,
        remaining_seconds = seconds,
        completes_at      = None if seconds is None else now + timedelta(seconds=seconds),
    )

def estimate_eta(facts: WorkloadFacts, rate: Rate | None, now: datetime) -> Eta:

    if facts.finished:
        return Eta(EtaKind.FINISHED, 0, 0.0, None)

    if facts.mode == WorkloadMode.SPRT and facts.sprt is not None:
        if (forecast := forecast_sprt(facts.outcomes, facts.llr, facts.sprt)) is None:
            return Eta(EtaKind.UNAVAILABLE, None, None, None)
        return with_rate(EtaKind.SPRT, forecast.remaining_games, rate, now)

    if (remaining := remaining_target_games(facts)) is not None:
        return with_rate(EtaKind.TARGET, remaining, rate, now)

    return Eta(EtaKind.UNAVAILABLE, None, None, None)
