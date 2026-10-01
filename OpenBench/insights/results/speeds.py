import math
from collections.abc import Sequence
from dataclasses import dataclass
from statistics import fmean, stdev

import scipy.stats

from OpenBench.insights.results.hosts import CpuTotals, HostTotals
from OpenBench.insights.speed import SpeedCounters, nodes_per_second

SPREAD_CONFIDENCE = 0.95
MIN_SPREAD_HOSTS = 2


@dataclass(frozen=True, slots=True)
class EngineSpeed:
    dev_nps: int | None
    base_nps: int | None
    dev_nps_scaled: int | None
    base_nps_scaled: int | None
    difference: float | None


@dataclass(frozen=True, slots=True)
class SpeedSpread:
    hosts: int
    mean: float
    lower: float
    upper: float
    beyond_noise: bool


@dataclass(frozen=True, slots=True)
class SpeedReading:
    difference: float
    speed: EngineSpeed
    spread: SpeedSpread | None


@dataclass(frozen=True, slots=True)
class CpuSpeed:
    cpu_name: str
    reading: SpeedReading


@dataclass(frozen=True, slots=True)
class SpeedComparison:
    overall: SpeedReading
    cpus: list[CpuSpeed]


def measured(nodes: int, time_ms: int) -> int | None:
    return nodes_per_second(nodes, time_ms) or None


def speed_ratio(counters: SpeedCounters) -> float | None:
    if not all((counters.dev_nodes, counters.dev_time, counters.base_nodes, counters.base_time)):
        return None
    return (counters.dev_nodes / counters.dev_time) / (counters.base_nodes / counters.base_time)


def engine_speed(counters: SpeedCounters) -> EngineSpeed:
    ratio = speed_ratio(counters)
    return EngineSpeed(
        dev_nps=measured(counters.dev_nodes, counters.dev_time),
        base_nps=measured(counters.base_nodes, counters.base_time),
        dev_nps_scaled=measured(counters.dev_nodes, counters.dev_time_scaled),
        base_nps_scaled=measured(counters.base_nodes, counters.base_time_scaled),
        difference=None if ratio is None else ratio - 1.0,
    )


def speed_spread(hosts: Sequence[HostTotals]) -> SpeedSpread | None:

    logs = [math.log(ratio) for host in hosts if (ratio := speed_ratio(host.tally.speed)) is not None]
    if len(logs) < MIN_SPREAD_HOSTS:
        return None

    centre = fmean(logs)
    tail = (1.0 + SPREAD_CONFIDENCE) / 2
    margin = float(scipy.stats.t.ppf(tail, len(logs) - 1)) * stdev(logs) / math.sqrt(len(logs))
    lower, upper = centre - margin, centre + margin

    return SpeedSpread(
        hosts=len(logs),
        mean=math.expm1(centre),
        lower=math.expm1(lower),
        upper=math.expm1(upper),
        beyond_noise=margin > 0.0 and (lower > 0.0 or upper < 0.0),
    )


def speed_reading(counters: SpeedCounters, hosts: Sequence[HostTotals]) -> SpeedReading | None:

    if (ratio := speed_ratio(counters)) is None:
        return None

    # The headline is the estimate the noise test is about: the mean over hosts once there are two
    spread = speed_spread(hosts)
    return SpeedReading(spread.mean if spread else ratio - 1.0, engine_speed(counters), spread)


def compare_speed(overall: SpeedCounters, cpus: Sequence[CpuTotals]) -> SpeedComparison | None:

    if (reading := speed_reading(overall, [host for cpu in cpus for host in cpu.hosts])) is None:
        return None

    return SpeedComparison(
        overall=reading,
        cpus=[
            CpuSpeed(cpu.cpu_name, cpu_reading)
            for cpu in cpus
            if (cpu_reading := speed_reading(cpu.tally.speed, cpu.hosts)) is not None
        ],
    )
