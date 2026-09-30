from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from OpenBench.insights.domain import ProgressPoint
from OpenBench.insights.strength import elo_interval


@dataclass(frozen=True, slots=True)
class SeriesPoint:
    timestamp: datetime
    games: int
    llr: float | None
    elo: float | None
    elo_lower: float | None
    elo_upper: float | None


def series_point(point: ProgressPoint, with_llr: bool, with_elo: bool) -> SeriesPoint:
    interval = elo_interval(point.outcomes.primary()) if with_elo else None
    return SeriesPoint(
        timestamp=point.timestamp,
        games=point.games,
        llr=point.llr if with_llr else None,
        elo=None if interval is None else interval.value,
        elo_lower=None if interval is None else interval.lower,
        elo_upper=None if interval is None else interval.upper,
    )


def build_series(points: Sequence[ProgressPoint], with_llr: bool, with_elo: bool) -> list[SeriesPoint]:
    return [series_point(point, with_llr, with_elo) for point in points]
