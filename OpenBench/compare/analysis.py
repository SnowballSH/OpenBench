import math
import re
from bisect import bisect_left
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from OpenBench.insights.series import SeriesPoint
from OpenBench.insights.strength import EloInterval

WORKLOAD_ID = re.compile(r'\d{1,18}')


@dataclass(frozen=True, slots=True)
class CompareQuery:
    a_text: str
    b_text: str
    a: int | None
    b: int | None
    errors: tuple[str, ...]

    @property
    def blank(self) -> bool:
        return not self.a_text and not self.b_text

    @property
    def pair(self) -> tuple[int, int] | None:
        if self.errors or self.a is None or self.b is None:
            return None
        return self.a, self.b


@dataclass(frozen=True, slots=True)
class EloSample:
    games: int
    value: float
    half_width: float


@dataclass(frozen=True, slots=True)
class Difference:
    games: int
    value: float
    lower: float
    upper: float


def parse_workload_id(raw: str) -> int | None:
    if not WORKLOAD_ID.fullmatch(raw) or (value := int(raw)) == 0:
        return None
    return value


def id_error(label: str, raw: str, value: int | None) -> str | None:
    if not raw:
        return f'Enter a workload id for {label}.'
    if value is None:
        return f'Workload {label} must be a positive whole number.'
    return None


def parse_query(params: Mapping[str, str]) -> CompareQuery:
    a_text, b_text = (params.get(key, '').strip()[:32] for key in ('a', 'b'))
    a, b = parse_workload_id(a_text), parse_workload_id(b_text)
    if not a_text and not b_text:
        return CompareQuery(a_text, b_text, None, None, ())
    errors = [error for error in (id_error('A', a_text, a), id_error('B', b_text, b)) if error]
    if a is not None and a == b:
        errors.append('Choose two different workloads.')
    return CompareQuery(a_text, b_text, a, b, tuple(errors))


def half_width(interval: EloInterval) -> float:
    return (interval.upper - interval.lower) / 2


def combined_half_width(first: float, second: float) -> float:
    # Independent samples: the variances add, so the half-widths add in quadrature
    return math.hypot(first, second)


def elo_difference(a: EloInterval | None, b: EloInterval | None) -> EloInterval | None:
    if a is None or b is None:
        return None
    value = a.value - b.value
    spread = combined_half_width(half_width(a), half_width(b))
    return EloInterval(lower=value - spread, value=value, upper=value + spread)


def elo_track(points: Sequence[SeriesPoint]) -> list[EloSample]:
    return [
        EloSample(point.games, point.elo, (point.elo_upper - point.elo_lower) / 2)
        for point in points
        if point.elo is not None and point.elo_lower is not None and point.elo_upper is not None
    ]


def sample_at(track: Sequence[EloSample], games: int) -> EloSample:
    index = bisect_left([sample.games for sample in track], games)
    if index == len(track):
        return track[-1]
    after = track[index]
    if after.games == games or index == 0:
        return after
    before = track[index - 1]
    share = (games - before.games) / (after.games - before.games)
    return EloSample(
        games,
        before.value + share * (after.value - before.value),
        before.half_width + share * (after.half_width - before.half_width),
    )


def difference_series(a_points: Sequence[SeriesPoint], b_points: Sequence[SeriesPoint]) -> list[Difference]:
    a_track, b_track = elo_track(a_points), elo_track(b_points)
    if not a_track or not b_track:
        return []

    first = max(a_track[0].games, b_track[0].games)
    last = min(a_track[-1].games, b_track[-1].games)
    grid = sorted({sample.games for sample in (*a_track, *b_track) if first <= sample.games <= last})

    differences = []
    for games in grid:
        a, b = sample_at(a_track, games), sample_at(b_track, games)
        value = a.value - b.value
        spread = combined_half_width(a.half_width, b.half_width)
        differences.append(Difference(games, value, value - spread, value + spread))
    return differences
