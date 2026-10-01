import math
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from itertools import combinations

from scipy.stats import t as student_t

from OpenBench.progress.domain import (
    MINIMUM_SAMPLE,
    ClassSpeed,
    HostCounters,
    RatioInterval,
    RunRow,
    SpeedPoint,
    SpeedRatio,
    SpeedSeries,
    Step,
    StepSpeed,
    TimeClass,
)

CONFIDENCE = 0.975
MS_PER_HOUR = 3_600_000


@dataclass(frozen=True, slots=True)
class Observation:
    log_ratio: float
    weight: float


def has_counters(counters: HostCounters) -> bool:
    return min(counters.dev_nodes, counters.dev_ms, counters.base_nodes, counters.base_ms) > 0


def log_speed_ratio(dev_nodes: int, dev_ms: int, base_nodes: int, base_ms: int) -> float:
    return math.log(dev_nodes * base_ms) - math.log(base_nodes * dev_ms)


def merge_hosts(counters: Iterable[HostCounters]) -> list[HostCounters]:
    merged: defaultdict[str, list[int]] = defaultdict(lambda: [0] * 6)
    for found in counters:
        totals = merged[found.host]
        values = (found.games, found.counted_games, found.dev_nodes, found.dev_ms, found.base_nodes, found.base_ms)
        for position, value in enumerate(values):
            totals[position] += value
    return [HostCounters(host, *totals) for host, totals in merged.items()]


def observations(hosts: Iterable[HostCounters]) -> list[Observation]:
    return [
        Observation(
            log_speed_ratio(host.dev_nodes, host.dev_ms, host.base_nodes, host.base_ms),
            float(host.dev_ms + host.base_ms),
        )
        for host in hosts
        if has_counters(host)
    ]


def weighted_mean(found: Sequence[Observation]) -> float:
    return sum(item.weight * item.log_ratio for item in found) / sum(item.weight for item in found)


def log_half_width(found: Sequence[Observation], mean: float) -> float | None:
    count = len(found)
    if count < 2:
        return None
    total = sum(item.weight for item in found)
    scatter = sum((item.weight * (item.log_ratio - mean)) ** 2 for item in found)
    variance = count / (count - 1) * scatter / total**2
    return float(student_t.ppf(CONFIDENCE, count - 1)) * math.sqrt(variance)


def speed_of(counters: Iterable[HostCounters]) -> SpeedRatio | None:
    hosts = [host for host in merge_hosts(counters) if has_counters(host)]
    games = sum(host.counted_games for host in hosts)
    if not hosts or games < MINIMUM_SAMPLE:
        return None
    found = observations(hosts)
    mean = weighted_mean(found)
    margin = log_half_width(found, mean)
    return SpeedRatio(
        ratio=math.exp(mean),
        lower=None if margin is None else math.exp(mean - margin),
        upper=None if margin is None else math.exp(mean + margin),
        hosts=len(hosts),
        games=games,
    )


def log_margin(speed: SpeedRatio | RatioInterval) -> float | None:
    if speed.lower is None or speed.upper is None:
        return None
    return (math.log(speed.upper) - math.log(speed.lower)) / 2


def disagree(first: SpeedRatio, second: SpeedRatio) -> bool:
    margins = log_margin(first), log_margin(second)
    if margins[0] is None or margins[1] is None:
        return False
    return abs(math.log(first.ratio / second.ratio)) > math.hypot(margins[0], margins[1])


def step_speed(rows: Sequence[RunRow]) -> StepSpeed | None:
    chained = [row for row in rows if row.time_class.chained]
    pooled = speed_of(host for row in chained for host in row.hosts)
    if pooled is None:
        return None
    classes = [
        ClassSpeed(time_class, found)
        for time_class in TimeClass
        if (found := speed_of(host for row in chained if row.time_class == time_class for host in row.hosts))
    ]
    return StepSpeed(
        pooled=pooled,
        classes=classes,
        classes_differ=any(disagree(first.speed, second.speed) for first, second in combinations(classes, 2)),
    )


def core_hours(row: RunRow) -> float | None:
    counted = [host for host in row.hosts if has_counters(host)]
    if not counted:
        return None
    return sum(host.dev_ms + host.base_ms for host in counted) * row.threads / MS_PER_HOUR


@dataclass(slots=True)
class RunningSpeed:
    measured: int = 0
    unbounded: int = 0
    log_ratio: float = 0.0
    variance: float = 0.0

    def add(self, speed: SpeedRatio) -> None:
        margin = log_margin(speed)
        self.measured += 1
        self.log_ratio += math.log(speed.ratio)
        if margin is None:
            self.unbounded += 1
        else:
            self.variance += margin**2

    def interval(self) -> RatioInterval | None:
        if not self.measured:
            return None
        if self.unbounded:
            return RatioInterval(math.exp(self.log_ratio), None, None)
        margin = math.sqrt(self.variance)
        return RatioInterval(
            math.exp(self.log_ratio), math.exp(self.log_ratio - margin), math.exp(self.log_ratio + margin)
        )


def speed_point(index: int, step: Step, running: RunningSpeed) -> SpeedPoint:
    if step.speed is None:
        return SpeedPoint(index, None, None)
    running.add(step.speed.pooled)
    return SpeedPoint(index, step.speed.pooled, running.interval())


def speed_series(steps: Sequence[Step], first_index: int) -> SpeedSeries:
    running = RunningSpeed()
    points = [speed_point(index, step, running) for index, step in enumerate(steps, start=first_index)]
    return SpeedSeries(
        points=points,
        total=running.interval(),
        measured=running.measured,
        unbounded=running.unbounded,
        steps=len(steps),
    )
