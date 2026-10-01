from collections.abc import Callable, Sequence
from dataclasses import dataclass

from OpenBench.insights.contributions import UNKNOWN_CPU, ResultRow, as_outcomes
from OpenBench.insights.domain import Outcomes
from OpenBench.insights.speed import SpeedCounters

type HostKey = tuple[str, str | None, str]

OUTCOME_COUNTERS = 8
SPEED_COUNTERS = 6


@dataclass(frozen=True, slots=True)
class Tally:
    outcomes: Outcomes
    crashes: int
    timelosses: int
    speed: SpeedCounters


@dataclass(frozen=True, slots=True)
class HostTotals:
    owner: str
    machine_name: str | None
    cpu_name: str
    machine_ids: tuple[int, ...]
    tally: Tally

    @property
    def newest_machine_id(self) -> int:
        return max(self.machine_ids)


@dataclass(frozen=True, slots=True)
class CpuTotals:
    cpu_name: str
    hosts: tuple[HostTotals, ...]
    tally: Tally


def host_key(row: ResultRow) -> HostKey:
    return row.owner, row.machine_name, row.cpu_name or UNKNOWN_CPU


def summed(columns: Sequence[Sequence[int]], width: int) -> list[int]:
    return [sum(values) for values in zip(*columns, strict=True)] or [0] * width


def row_tally(row: ResultRow) -> Tally:
    return Tally(row.outcomes, row.crashes, row.timelosses, row.speed)


def combine(tallies: Sequence[Tally], use_penta: bool) -> Tally:
    return Tally(
        outcomes=as_outcomes(
            summed([(*tally.outcomes.trinomial, *tally.outcomes.pentanomial) for tally in tallies], OUTCOME_COUNTERS),
            use_penta,
        ),
        crashes=sum(tally.crashes for tally in tallies),
        timelosses=sum(tally.timelosses for tally in tallies),
        speed=SpeedCounters(*summed([tally.speed.as_tuple() for tally in tallies], SPEED_COUNTERS)),
    )


def grouped[R, K](items: Sequence[R], key_of: Callable[[R], K]) -> dict[K, list[R]]:
    groups: dict[K, list[R]] = {}
    for item in items:
        groups.setdefault(key_of(item), []).append(item)
    return groups


def games_of(totals: HostTotals | CpuTotals) -> int:
    return totals.tally.outcomes.games


def host_totals(key: HostKey, rows: Sequence[ResultRow], use_penta: bool) -> HostTotals:
    owner, machine_name, cpu_name = key
    return HostTotals(
        owner=owner,
        machine_name=machine_name,
        cpu_name=cpu_name,
        machine_ids=tuple(sorted({row.machine_id for row in rows})),
        tally=combine([row_tally(row) for row in rows], use_penta),
    )


def group_by_host(rows: Sequence[ResultRow], use_penta: bool) -> list[HostTotals]:
    hosts = [host_totals(key, members, use_penta) for key, members in grouped(rows, host_key).items()]
    return sorted(hosts, key=games_of, reverse=True)


def group_by_cpu(hosts: Sequence[HostTotals], use_penta: bool) -> list[CpuTotals]:
    cpus = [
        CpuTotals(cpu_name, tuple(members), combine([host.tally for host in members], use_penta))
        for cpu_name, members in grouped(hosts, lambda host: host.cpu_name).items()
    ]
    return sorted(cpus, key=games_of, reverse=True)
