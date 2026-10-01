from collections.abc import Callable, Sequence
from dataclasses import dataclass

from OpenBench.fleet.hosts import HostKey
from OpenBench.fleet.pools import pool_label, short_name
from OpenBench.insights.domain import Outcomes
from OpenBench.insights.grouping import sum_by_key
from OpenBench.insights.speed import SpeedCounters
from OpenBench.insights.strength import EloInterval, elo_interval

UNKNOWN_CPU = 'Unknown'


@dataclass(frozen=True, slots=True)
class ResultRow:
    machine_id: int
    host: HostKey
    machine_name: str | None
    owner: str
    cpu_name: str | None
    outcomes: Outcomes
    crashes: int = 0
    timelosses: int = 0
    speed: SpeedCounters = SpeedCounters()


@dataclass(frozen=True, slots=True)
class Contribution:
    games: int
    pairs: int
    share: float | None
    pairs_per_hour: float | None
    elo: EloInterval | None


@dataclass(frozen=True, slots=True)
class Registration:
    machine_id: int
    games: int
    pairs: int


@dataclass(frozen=True, slots=True)
class MachineContribution:
    machine_id: int
    machine_name: str | None
    machine_label: str | None
    pool: str
    owner: str
    cpu_name: str
    registrations: list[Registration]
    stats: Contribution


@dataclass(frozen=True, slots=True)
class CpuContribution:
    cpu_name: str
    machines: int
    stats: Contribution


@dataclass(frozen=True, slots=True)
class Contributions:
    machines: list[MachineContribution]
    cpus: list[CpuContribution]


def counters(row: ResultRow) -> tuple[int, ...]:
    return (*row.outcomes.trinomial, *row.outcomes.pentanomial)


def as_outcomes(totals: Sequence[int], use_penta: bool) -> Outcomes:
    return Outcomes(
        (totals[0], totals[1], totals[2]), (totals[3], totals[4], totals[5], totals[6], totals[7]), use_penta
    )


def contribution(outcomes: Outcomes, total_games: int, elapsed_seconds: float | None) -> Contribution:
    return Contribution(
        games=outcomes.games,
        pairs=outcomes.pairs,
        share=outcomes.games / total_games if total_games else None,
        pairs_per_hour=3600 * outcomes.pairs / elapsed_seconds if elapsed_seconds else None,
        elo=elo_interval(outcomes.primary()),
    )


def grouped[K](
    rows: Sequence[ResultRow], key_of: Callable[[ResultRow], K | None], missing: K, use_penta: bool
) -> dict[K, Outcomes]:
    return {key: as_outcomes(totals, use_penta) for key, totals in sum_by_key(rows, key_of, counters, missing).items()}


def by_games[T](items: list[T], games_of: Callable[[T], int]) -> list[T]:
    return sorted(items, key=games_of, reverse=True)


def registrations_by_host(rows: Sequence[ResultRow]) -> dict[HostKey, list[ResultRow]]:
    hosts: dict[HostKey, list[ResultRow]] = {}
    for row in sorted(rows, key=lambda row: row.machine_id, reverse=True):
        hosts.setdefault(row.host, []).append(row)
    return hosts


def machine_contribution(newest_first: Sequence[ResultRow], stats: Contribution) -> MachineContribution:
    newest = newest_first[0]
    return MachineContribution(
        machine_id=newest.machine_id,
        machine_name=newest.machine_name,
        machine_label=short_name(newest.machine_name) if newest.machine_name else None,
        pool=pool_label(newest.machine_name, newest.cpu_name or UNKNOWN_CPU),
        owner=newest.owner,
        cpu_name=newest.cpu_name or UNKNOWN_CPU,
        registrations=[Registration(row.machine_id, row.outcomes.games, row.outcomes.pairs) for row in newest_first],
        stats=stats,
    )


def summarize_contributions(rows: Sequence[ResultRow], use_penta: bool, elapsed_seconds: float | None) -> Contributions:

    total = sum(row.outcomes.games for row in rows)
    hosts = registrations_by_host(rows)
    pooled = grouped(rows, lambda row: row.host, HostKey(''), use_penta)

    machines = [
        machine_contribution(registrations, contribution(pooled[host], total, elapsed_seconds))
        for host, registrations in hosts.items()
    ]

    host_counts = sum_by_key(
        (registrations[0] for registrations in hosts.values()), lambda row: row.cpu_name, lambda _: (1,), UNKNOWN_CPU
    )
    cpus = [
        CpuContribution(cpu_name, host_counts[cpu_name][0], contribution(outcomes, total, elapsed_seconds))
        for cpu_name, outcomes in grouped(rows, lambda row: row.cpu_name, UNKNOWN_CPU, use_penta).items()
    ]

    return Contributions(
        machines=by_games(machines, lambda item: item.stats.games),
        cpus=by_games(cpus, lambda item: item.stats.games),
    )
