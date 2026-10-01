from collections.abc import Sequence
from datetime import datetime

from OpenBench.digest.domain import MOVES_SENT, ClassMovement, DigestWindow, TrunkMove, TrunkMovement
from OpenBench.progress.domain import Lineage, Measurement, RunRow, RunStatus, Step, TimeClass
from OpenBench.progress.lineage import chain_series
from OpenBench.progress.report import engine_lineage, engines_with_steps


def settled_at(measurement: Measurement) -> datetime | None:
    return max((run.finished_at for run in measurement.runs if run.finished_at is not None), default=None)


def moved(measurement: Measurement, window: DigestWindow) -> bool:
    return measurement.provisional or window.holds(settled_at(measurement))


def trunk_move(index: int, step: Step, measurement: Measurement) -> TrunkMove:
    return TrunkMove(
        index=index,
        base=step.base,
        dev=step.dev,
        repo=step.repo,
        subject=step.subject,
        verdict=measurement.verdict,
        provisional=measurement.provisional,
        elo=measurement.elo,
        games=measurement.games,
        measured_at=None if measurement.provisional else settled_at(measurement),
        runs=[run.id for run in measurement.runs],
    )


def class_movement(time_class: TimeClass, trunk: Sequence[Step], window: DigestWindow) -> ClassMovement | None:
    found = [
        (index, step, measurement)
        for index, step in enumerate(trunk, start=1)
        if (measurement := step.measurement(time_class)) is not None and moved(measurement, window)
    ]
    if not found:
        return None

    series = chain_series([step for _, step, _ in found], time_class, 1)
    moves = [trunk_move(index, step, measurement) for index, step, measurement in found]
    return ClassMovement(
        time_class=time_class,
        moves=moves[-MOVES_SENT:],
        moves_omitted=max(0, len(moves) - MOVES_SENT),
        measured=series.measured,
        accepted=sum(move.verdict == RunStatus.PASSED and not move.provisional for move in moves),
        provisional=sum(move.provisional for move in moves),
        net=series.total,
    )


def lineage_movement(engine: str, lineage: Lineage, window: DigestWindow) -> TrunkMovement | None:
    classes = [
        found
        for time_class in TimeClass
        if time_class.chained and (found := class_movement(time_class, lineage.trunk, window)) is not None
    ]
    if not classes:
        return None
    return TrunkMovement(engine=engine, head=lineage.head, trunk_length=len(lineage.trunk), classes=classes)


def trunk_movements(runs: list[RunRow], window: DigestWindow) -> list[TrunkMovement]:
    return [
        found
        for engine in engines_with_steps(runs)
        if (lineage := engine_lineage(runs, engine)) is not None
        and (found := lineage_movement(engine, lineage, window)) is not None
    ]
