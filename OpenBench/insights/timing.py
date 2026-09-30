from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

type Mark = tuple[datetime, int]

RECENT_WINDOW = timedelta(hours=1)
MIN_RATE_SPAN = timedelta(minutes=5)


@dataclass(frozen=True, slots=True)
class Rate:
    games_per_hour: float
    window_seconds: float


@dataclass(frozen=True, slots=True)
class TimingSummary:
    started_at: datetime
    ended_at: datetime | None
    elapsed_seconds: float
    overall: Rate | None
    recent: Rate | None

    def best_rate(self) -> Rate | None:
        return self.recent or self.overall


def games_at(marks: Sequence[Mark], when: datetime) -> float | None:

    if not marks or when < marks[0][0]:
        return None

    index = bisect_right([timestamp for timestamp, _ in marks], when)
    if index == len(marks):
        return float(marks[-1][1])

    (t0, g0), (t1, g1) = marks[index - 1], marks[index]
    if t1 == t0:
        return float(g1)
    return g0 + (g1 - g0) * ((when - t0) / (t1 - t0))


def rate_between(marks: Sequence[Mark], start: datetime, end: datetime) -> Rate | None:

    start = max(start, marks[0][0]) if marks else start
    span = end - start

    if span < MIN_RATE_SPAN:
        return None

    first, last = games_at(marks, start), games_at(marks, end)
    if first is None or last is None:
        return None

    seconds = span.total_seconds()
    return Rate(games_per_hour=3600 * (last - first) / seconds, window_seconds=seconds)


def summarize_timing(marks: Sequence[Mark], end: datetime, finished: bool) -> TimingSummary | None:

    if not marks:
        return None

    started_at = marks[0][0]
    return TimingSummary(
        started_at=started_at,
        ended_at=end if finished else None,
        elapsed_seconds=max(0.0, (end - started_at).total_seconds()),
        overall=rate_between(marks, started_at, end),
        recent=rate_between(marks, end - RECENT_WINDOW, end),
    )
