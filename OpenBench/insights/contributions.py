from collections.abc import Callable, Sequence
from dataclasses import dataclass

from OpenBench.insights.domain import Outcomes
from OpenBench.insights.grouping import sum_by_key
from OpenBench.insights.strength import EloInterval, elo_interval

UNKNOWN_CPU = 'Unknown'

@dataclass(frozen=True, slots=True)
class ResultRow:
    machine_id   : int
    machine_name : str | None
    owner        : str
    cpu_name     : str | None
    outcomes     : Outcomes

@dataclass(frozen=True, slots=True)
class Contribution:
    games          : int
    pairs          : int
    share          : float | None
    pairs_per_hour : float | None
    elo            : EloInterval | None

@dataclass(frozen=True, slots=True)
class MachineContribution:
    machine_id   : int
    machine_name : str | None
    owner        : str
    cpu_name     : str
    stats        : Contribution

@dataclass(frozen=True, slots=True)
class CpuContribution:
    cpu_name     : str
    machines     : int
    stats        : Contribution

@dataclass(frozen=True, slots=True)
class Contributions:
    machines : list[MachineContribution]
    cpus     : list[CpuContribution]

def counters(row: ResultRow) -> tuple[int, ...]:
    return (*row.outcomes.trinomial, *row.outcomes.pentanomial)

def as_outcomes(totals: Sequence[int], use_penta: bool) -> Outcomes:
    return Outcomes((totals[0], totals[1], totals[2]), (totals[3], totals[4], totals[5], totals[6], totals[7]), use_penta)

def contribution(outcomes: Outcomes, total_games: int, elapsed_seconds: float | None) -> Contribution:
    return Contribution(
        games          = outcomes.games,
        pairs          = outcomes.pairs,
        share          = outcomes.games / total_games if total_games else None,
        pairs_per_hour = 3600 * outcomes.pairs / elapsed_seconds if elapsed_seconds else None,
        elo            = elo_interval(outcomes.primary()),
    )

def grouped[K](rows: Sequence[ResultRow], key_of: Callable[[ResultRow], K | None], missing: K, use_penta: bool) -> dict[K, Outcomes]:
    return { key : as_outcomes(totals, use_penta) for key, totals in sum_by_key(rows, key_of, counters, missing).items() }

def by_games[T](items: list[T], games_of: Callable[[T], int]) -> list[T]:
    return sorted(items, key=games_of, reverse=True)

def summarize_contributions(rows: Sequence[ResultRow], use_penta: bool, elapsed_seconds: float | None) -> Contributions:

    total = sum(row.outcomes.games for row in rows)
    first = { row.machine_id : row for row in reversed(rows) }

    machines = [
        MachineContribution(
            machine_id   = machine_id,
            machine_name = first[machine_id].machine_name,
            owner        = first[machine_id].owner,
            cpu_name     = first[machine_id].cpu_name or UNKNOWN_CPU,
            stats        = contribution(outcomes, total, elapsed_seconds),
        )
        for machine_id, outcomes in grouped(rows, lambda row: row.machine_id, 0, use_penta).items()
    ]

    machine_counts = sum_by_key(first.values(), lambda row: row.cpu_name, lambda _: (1,), UNKNOWN_CPU)
    cpus = [
        CpuContribution(cpu_name, machine_counts[cpu_name][0], contribution(outcomes, total, elapsed_seconds))
        for cpu_name, outcomes in grouped(rows, lambda row: row.cpu_name, UNKNOWN_CPU, use_penta).items()
    ]

    return Contributions(
        machines = by_games(machines, lambda item: item.stats.games),
        cpus     = by_games(cpus, lambda item: item.stats.games),
    )
