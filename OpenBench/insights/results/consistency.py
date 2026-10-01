import math
from collections.abc import Sequence
from dataclasses import dataclass

import scipy.stats

from OpenBench.insights.results.hosts import CpuTotals, HostTotals, Tally
from OpenBench.insights.results.speeds import EngineSpeed, engine_speed
from OpenBench.insights.strength import NORMAL, EloInterval, elo_interval, score_moments

MIN_GROUP_SAMPLES = 200
DEVIATION_ALPHA = 0.001
CRASH_RATE_LIMIT = 0.001
TIMELOSS_RATE_LIMIT = 0.005
MIN_FAULTS = 2

type Sample = tuple[int, ...]


@dataclass(frozen=True, slots=True)
class Deviation:
    z_score: float
    p_value: float
    adjusted_p_value: float
    flagged: bool


@dataclass(frozen=True, slots=True)
class GroupStats:
    games: int
    pairs: int
    elo: EloInterval | None
    deviation: Deviation | None
    crashes: int
    timelosses: int
    crash_rate: float | None
    timeloss_rate: float | None
    crash_flagged: bool
    timeloss_flagged: bool
    speed: EngineSpeed


@dataclass(frozen=True, slots=True)
class CpuResult:
    cpu_name: str
    hosts: int
    stats: GroupStats


@dataclass(frozen=True, slots=True)
class HostResult:
    owner: str
    machine_name: str | None
    machine_label: str | None
    pool: str
    cpu_name: str
    machine_id: int
    machines: int
    stats: GroupStats


@dataclass(frozen=True, slots=True)
class Heterogeneity:
    statistic: float
    degrees_of_freedom: int
    p_value: float
    pooled_small_groups: int
    flagged: bool


@dataclass(frozen=True, slots=True)
class HostCheck:
    total: int
    tested: int
    flagged: list[HostResult]


@dataclass(frozen=True, slots=True)
class Consistency:
    min_samples: int
    alpha: float
    cpus: list[CpuResult]
    heterogeneity: Heterogeneity | None
    hosts: HostCheck


def total_of(samples: Sequence[Sample]) -> Sample:
    return tuple(sum(values) for values in zip(*samples, strict=True))


def large_enough(sample: Sample) -> bool:
    return sum(sample) >= MIN_GROUP_SAMPLES


def z_against_rest(sample: Sample, pooled: Sample, variance: float) -> float | None:

    rest = tuple(total - part for total, part in zip(pooled, sample, strict=True))
    if not variance or not large_enough(sample) or not large_enough(rest):
        return None

    mine, others = score_moments(sample), score_moments(rest)
    if mine is None or others is None:
        return None

    return (mine.mean - others.mean) / math.sqrt(variance * (1 / mine.count + 1 / others.count))


def deviation(z_score: float | None, comparisons: int) -> Deviation | None:

    if z_score is None:
        return None

    # Bonferroni keeps the chance of any false flag in this family of comparisons below DEVIATION_ALPHA
    p_value = 2 * NORMAL.cdf(-abs(z_score))
    adjusted = min(1.0, p_value * comparisons)
    return Deviation(z_score, p_value, adjusted, flagged=adjusted < DEVIATION_ALPHA)


def deviations(samples: Sequence[Sample]) -> list[Deviation | None]:

    if not samples:
        return []

    pooled = total_of(samples)
    moments = score_moments(pooled)
    variance = moments.variance if moments else 0.0
    z_scores = [z_against_rest(sample, pooled, variance) for sample in samples]
    comparisons = sum(z is not None for z in z_scores)
    return [deviation(z, comparisons) for z in z_scores]


def with_small_groups_pooled(samples: Sequence[Sample]) -> tuple[list[Sample], int]:

    large = [sample for sample in samples if large_enough(sample)]
    small = [sample for sample in samples if not large_enough(sample)]
    if small and large_enough(pooled := total_of(small)):
        return [*large, pooled], len(small)
    return large, 0


def heterogeneity(samples: Sequence[Sample]) -> Heterogeneity | None:

    groups, pooled_small = with_small_groups_pooled(samples)
    if len(groups) < 2 or not (overall := score_moments(total_of(groups))) or not overall.variance:
        return None

    moments = [present for group in groups if (present := score_moments(group))]
    statistic = sum(group.count * (group.mean - overall.mean) ** 2 for group in moments) / overall.variance
    p_value = float(scipy.stats.chi2.sf(statistic, len(groups) - 1))
    return Heterogeneity(statistic, len(groups) - 1, p_value, pooled_small, flagged=p_value < DEVIATION_ALPHA)


def rate(count: int, games: int) -> float | None:
    return count / games if games else None


def exceeds(count: int, games: int, limit: float) -> bool:
    return count >= MIN_FAULTS and games > 0 and count / games > limit


def crashes_too_often(tally: Tally) -> bool:
    return exceeds(tally.crashes, tally.outcomes.games, CRASH_RATE_LIMIT)


def loses_on_time_too_often(tally: Tally) -> bool:
    return exceeds(tally.timelosses, tally.outcomes.games, TIMELOSS_RATE_LIMIT)


def stands_out(tally: Tally, found: Deviation | None) -> bool:
    return bool(found and found.flagged) or crashes_too_often(tally) or loses_on_time_too_often(tally)


def group_stats(tally: Tally, found: Deviation | None) -> GroupStats:
    games = tally.outcomes.games
    return GroupStats(
        games=games,
        pairs=tally.outcomes.pairs,
        elo=elo_interval(tally.outcomes.primary()),
        deviation=found,
        crashes=tally.crashes,
        timelosses=tally.timelosses,
        crash_rate=rate(tally.crashes, games),
        timeloss_rate=rate(tally.timelosses, games),
        crash_flagged=crashes_too_often(tally),
        timeloss_flagged=loses_on_time_too_often(tally),
        speed=engine_speed(tally.speed),
    )


def host_result(host: HostTotals, found: Deviation | None) -> HostResult:
    return HostResult(
        owner=host.owner,
        machine_name=host.machine_name,
        machine_label=host.machine_label,
        pool=host.pool,
        cpu_name=host.cpu_name,
        machine_id=host.newest_machine_id,
        machines=len(host.machine_ids),
        stats=group_stats(host.tally, found),
    )


def check_hosts(hosts: Sequence[HostTotals]) -> HostCheck:
    found = deviations([host.tally.outcomes.primary() for host in hosts])
    return HostCheck(
        total=len(hosts),
        tested=sum(deviation is not None for deviation in found),
        flagged=[
            host_result(host, deviation)
            for host, deviation in zip(hosts, found, strict=True)
            if stands_out(host.tally, deviation)
        ],
    )


def check_consistency(cpus: Sequence[CpuTotals]) -> Consistency:
    samples = [cpu.tally.outcomes.primary() for cpu in cpus]
    return Consistency(
        min_samples=MIN_GROUP_SAMPLES,
        alpha=DEVIATION_ALPHA,
        cpus=[
            CpuResult(cpu.cpu_name, len(cpu.hosts), group_stats(cpu.tally, found))
            for cpu, found in zip(cpus, deviations(samples), strict=True)
        ],
        heterogeneity=heterogeneity(samples),
        hosts=check_hosts([host for cpu in cpus for host in cpu.hosts]),
    )
