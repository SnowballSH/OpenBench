from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from OpenBench.insights.domain import WorkloadFacts
from OpenBench.insights.eta import Eta, EtaKind, EtaReason, timing_and_eta
from OpenBench.insights.timing import Mark, Rate, timeline_marks

ETA_REASON_TEXT: dict[EtaReason, str] = {
    EtaReason.TOO_FEW_GAMES: 'needs 200 games first',
    EtaReason.OUTSIDE_BOUNDS: 'LLR is outside the bounds',
    EtaReason.EMPTY_OUTCOME: 'needs wins, draws and losses',
    EtaReason.NO_VARIANCE: 'results too uniform to project',
    EtaReason.NO_TARGET: 'no target to reach',
    EtaReason.NO_RATE: 'no recent throughput',
}
SILENT_REASONS = frozenset({EtaReason.NO_TARGET})
ESTIMATE_NOTE = 'Assumes the test keeps producing results like it has so far; an order of magnitude, not a promise.'


class RowTimingKind(StrEnum):
    TOOK = 'took'
    LEFT = 'left'
    UNAVAILABLE = 'unavailable'
    RATE_ONLY = 'rate'


@dataclass(frozen=True, slots=True)
class SnapshotMarks:
    first: Mark | None
    window_before: Mark | None
    window_after: Mark | None
    latest: Mark | None

    def marks(self) -> list[Mark]:
        # The first snapshot, the pair bracketing the recent window's start, and the newest one: every
        # timestamp games_at is asked about falls between the same neighbours as in the full history
        chosen = (self.first, self.window_before, self.window_after, self.latest)
        return sorted({mark for mark in chosen if mark is not None})


@dataclass(frozen=True, slots=True)
class RowTiming:
    kind: RowTimingKind
    text: str | None
    estimate: bool = False
    rate: str | None = None
    rate_window: str | None = None

    @property
    def note(self) -> str | None:
        return ESTIMATE_NOTE if self.estimate else None


def format_duration(seconds: float) -> str:

    total = max(0, round(seconds))
    days, hours, minutes = total // 86400, total % 86400 // 3600, total % 3600 // 60

    if days:
        return f'{days}d {hours}h' if hours else f'{days}d'
    if hours:
        return f'{hours}h {minutes}m' if minutes else f'{hours}h'
    if minutes:
        return f'{minutes}m'
    return f'{total}s'


def format_rate(games_per_hour: float) -> str:
    return f'{round(games_per_hour):,}' if games_per_hour >= 100 else f'{games_per_hour:.1f}'


def rate_window(rate: Rate, overall: bool) -> str:
    return 'overall' if overall else f'last {format_duration(rate.window_seconds)}'


def eta_parts(eta: Eta) -> tuple[RowTimingKind, str | None]:

    if eta.remaining_seconds is not None:
        return RowTimingKind.LEFT, f'{format_duration(eta.remaining_seconds)} left'
    if eta.reason is not None and eta.reason not in SILENT_REASONS:
        return RowTimingKind.UNAVAILABLE, ETA_REASON_TEXT[eta.reason]
    return RowTimingKind.RATE_ONLY, None


def running_row_timing(facts: WorkloadFacts, snapshots: SnapshotMarks, now: datetime) -> RowTiming | None:

    current = (facts.updated_at, facts.outcomes.games)
    timing, eta = timing_and_eta(facts, timeline_marks(facts.created_at, current, snapshots.marks()), now)
    rate = timing.best_rate() if timing else None
    shown_rate = rate if rate and rate.games_per_hour > 0 else None
    kind, text = eta_parts(eta)

    if text is None and shown_rate is None:
        return None

    return RowTiming(
        kind=kind,
        text=text,
        estimate=kind == RowTimingKind.LEFT and eta.kind == EtaKind.SPRT,
        rate=f'{format_rate(shown_rate.games_per_hour)} games/h' if shown_rate else None,
        rate_window=rate_window(shown_rate, timing is not None and timing.recent is None) if shown_rate else None,
    )


def finished_row_timing(started_at: datetime, finished_at: datetime) -> RowTiming:
    elapsed = max(0.0, (finished_at - started_at).total_seconds())
    return RowTiming(kind=RowTimingKind.TOOK, text=f'took {format_duration(elapsed)}')
