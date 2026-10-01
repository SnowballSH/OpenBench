from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from OpenBench.diagnosis import scheduler
from OpenBench.diagnosis.domain import Fleet, Obstacle
from OpenBench.diagnosis.eligibility import obstacles
from OpenBench.models import Machine, Test


class StandingKind(StrEnum):
    NEXT = 'next'
    OUTRANKED = 'outranked'
    FOCUS = 'focus'
    LOW_SHARE = 'low_share'


@dataclass(frozen=True, slots=True)
class Rival:
    workload: Test
    threads: int | None = None


@dataclass(frozen=True, slots=True)
class Standing:
    kind: StandingKind
    rivals: tuple[Rival, ...] = ()
    threads: int = 0


class FleetJudge:
    def __init__(self, fleet: Fleet) -> None:
        self.fleet = fleet
        self._obstacles: dict[tuple[int, int], tuple[Obstacle, ...]] = {}

    def obstacles(self, workload: Test, machine: Machine) -> tuple[Obstacle, ...]:
        key = (workload.id, machine.id)
        if key not in self._obstacles:
            found = obstacles(workload, machine, self.fleet.configs, self.fleet.build_failures)
            self._obstacles[key] = tuple(found)
        return self._obstacles[key]

    def options(self, machine: Machine) -> list[Test]:
        return [workload for workload in self.fleet.active if not self.obstacles(workload, machine)]

    def standing(self, workload: Test, machine: Machine) -> Standing | None:
        # A throughput of zero cannot be ranked, and makes the scheduler itself fail for this machine
        try:
            return self.ranked(workload, machine)
        except ArithmeticError:
            return None

    def ranked(self, workload: Test, machine: Machine) -> Standing:

        options = self.options(machine)
        candidates, has_focus = scheduler.refine(options, machine)

        if workload.id not in {candidate.id for candidate in candidates}:
            top = max(option.priority for option in options)
            if workload.priority < top:
                return Standing(
                    StandingKind.OUTRANKED, rivals_of(option for option in options if option.priority == top)
                )
            return Standing(StandingKind.FOCUS, rivals_of(candidates))

        candidate_ids = {candidate.id for candidate in candidates}
        others = [other for other in self.fleet.assigned if other.workload in candidate_ids and other.id != machine.id]
        shares = scheduler.resource_ratios(candidates, machine, has_focus, others)
        ours = shares[workload.id]
        ahead = tuple(
            Rival(candidate, share['threads'])
            for candidate in candidates
            if (share := shares[candidate.id])['ratio'] < ours['ratio']
        )
        return Standing(StandingKind.LOW_SHARE if ahead else StandingKind.NEXT, ahead, ours['threads'])


def rivals_of(workloads: Iterable[Test]) -> tuple[Rival, ...]:
    return tuple(Rival(workload) for workload in workloads)
