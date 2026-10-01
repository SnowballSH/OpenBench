import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from functools import lru_cache
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
MINIMUM_FREEDOM = 0.5
WIDEST_LOG_MARGIN = 5.0
FREEDOM_STEPS = 100
MS_PER_HOUR = 3_600_000

type HostTotals = dict[str, list[int]]


@dataclass(frozen=True, slots=True)
class Observation:
    log_ratio: float
    weight: float


def has_counters(counters: HostCounters) -> bool:
    return counters.dev_nodes > 0 and counters.dev_ms > 0 and counters.base_nodes > 0 and counters.base_ms > 0


def merge_hosts(counters: Iterable[HostCounters]) -> HostTotals:
    merged: dict[str, list[int]] = {}
    for found in counters:
        if not has_counters(found):
            continue
        totals = merged.get(found.host)
        if totals is None:
            merged[found.host] = [
                found.counted_games,
                found.dev_nodes,
                found.dev_ms,
                found.base_nodes,
                found.base_ms,
            ]
        else:
            totals[0] += found.counted_games
            totals[1] += found.dev_nodes
            totals[2] += found.dev_ms
            totals[3] += found.base_nodes
            totals[4] += found.base_ms
    return merged


def merge_totals(groups: Iterable[HostTotals]) -> HostTotals:
    merged: dict[str, list[int]] = {}
    for group in groups:
        for host, totals in group.items():
            known = merged.get(host)
            merged[host] = list(totals) if known is None else [a + b for a, b in zip(known, totals, strict=True)]
    return merged


def observations(hosts: HostTotals) -> list[Observation]:
    return [
        Observation(math.log(dev_nodes * base_ms / (base_nodes * dev_ms)), float(dev_ms + base_ms))
        for _, dev_nodes, dev_ms, base_nodes, base_ms in hosts.values()
    ]


def weighted_mean(found: Sequence[Observation]) -> float:
    return sum(item.weight * item.log_ratio for item in found) / sum(item.weight for item in found)


def effective_freedom(found: Sequence[Observation]) -> float:
    total = sum(item.weight for item in found)
    return total**2 / sum(item.weight**2 for item in found) - 1


@lru_cache(maxsize=4096)
def quantile(freedom_steps: int) -> float:
    # Rounding the degrees of freedom down only widens the interval
    return float(student_t.ppf(CONFIDENCE, freedom_steps / FREEDOM_STEPS))


def log_half_width(found: Sequence[Observation], mean: float) -> float | None:
    count = len(found)
    if count < 2 or (freedom := effective_freedom(found)) < MINIMUM_FREEDOM:
        return None
    total = sum(item.weight for item in found)
    scatter = sum((item.weight * (item.log_ratio - mean)) ** 2 for item in found)
    variance = count / (count - 1) * scatter / total**2
    margin = quantile(math.floor(freedom * FREEDOM_STEPS)) * math.sqrt(variance)
    return margin if margin < WIDEST_LOG_MARGIN else None


def pooled_speed(hosts: HostTotals) -> SpeedRatio | None:
    games = sum(totals[0] for totals in hosts.values())
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


def speed_of(counters: Iterable[HostCounters]) -> SpeedRatio | None:
    return pooled_speed(merge_hosts(counters))


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
    by_class = {
        time_class: merge_hosts(host for row in rows if row.time_class == time_class for host in row.hosts)
        for time_class in TimeClass
        if time_class.chained
    }
    pooled = pooled_speed(merge_totals(by_class.values()))
    if pooled is None:
        return None
    classes = [
        ClassSpeed(time_class, found) for time_class, hosts in by_class.items() if (found := pooled_speed(hosts))
    ]
    return StepSpeed(
        pooled=pooled,
        classes=classes,
        classes_differ=any(disagree(first.speed, second.speed) for first, second in combinations(classes, 2)),
    )


@dataclass(frozen=True, slots=True)
class RunUsage:
    counted_games: int
    core_hours: float | None


def run_usage(row: RunRow) -> RunUsage:
    games, thought = 0, 0
    for host in row.hosts:
        if has_counters(host):
            games += host.counted_games
            thought += host.dev_ms * row.dev_threads + host.base_ms * row.base_threads
    return RunUsage(games, thought / MS_PER_HOUR if games or thought else None)


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
