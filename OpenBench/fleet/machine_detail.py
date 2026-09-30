from dataclasses import dataclass
from datetime import datetime

from django.db.models import Count, Sum

from OpenBench.fleet.machines import MachineRow, load_workloads, machine_row
from OpenBench.fleet.status import relative_age
from OpenBench.insights.domain import Outcomes, WorkloadMode
from OpenBench.insights.strength import EloInterval, elo_interval
from OpenBench.models import Machine, Result, Test

CONTRIBUTION_LIMIT = 50


@dataclass(frozen=True, slots=True)
class WorkloadContribution:
    test: Test
    games: int
    elo: EloInterval | None
    nps: int | None
    updated: datetime
    updated_ago: str

    @property
    def elo_margin(self) -> float | None:
        return (self.elo.upper - self.elo.lower) / 2 if self.elo else None


@dataclass(frozen=True, slots=True)
class MachineDetail:
    machine: Machine
    row: MachineRow
    workloads_total: int
    contributions: list[WorkloadContribution]

    @property
    def truncated(self) -> bool:
        return self.workloads_total > len(self.contributions)


def result_outcomes(result: Result) -> Outcomes:
    return Outcomes(
        (result.losses, result.draws, result.wins),
        (result.LL, result.LD, result.DD, result.DW, result.WW),
        not result.test.use_tri,
    )


def result_elo(result: Result) -> EloInterval | None:
    if result.test.test_mode == WorkloadMode.SPSA:
        return None
    return elo_interval(result_outcomes(result).primary())


def nodes_per_second(nodes: int, milliseconds: int) -> int | None:
    return round(1000 * nodes / milliseconds) if nodes and milliseconds else None


def workload_contribution(result: Result, now: datetime) -> WorkloadContribution:
    return WorkloadContribution(
        test=result.test,
        games=result.games,
        elo=result_elo(result),
        nps=nodes_per_second(result.dev_nodes, result.dev_time),
        updated=result.updated,
        updated_ago=relative_age(now - result.updated),
    )


def load_machine_detail(
    machine_id: int, now: datetime, limit: int = CONTRIBUTION_LIMIT
) -> MachineDetail | None:
    if not (
        machine := Machine.objects.select_related("user").filter(id=machine_id).first()
    ):
        return None

    results = Result.objects.filter(machine=machine)
    totals = results.aggregate(workloads=Count("id"), games=Sum("games", default=0))
    recent = results.select_related("test__dev").order_by("-updated", "-id")[:limit]

    return MachineDetail(
        machine=machine,
        row=machine_row(
            machine, totals["games"], load_workloads([machine.workload]), now
        ),
        workloads_total=totals["workloads"],
        contributions=[workload_contribution(result, now) for result in recent],
    )
