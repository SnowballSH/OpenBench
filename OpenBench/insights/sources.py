from typing import Any

from OpenBench.insights.contributions import ResultRow
from OpenBench.insights.domain import Outcomes, ProgressPoint, SprtBounds, WorkloadFacts, WorkloadMode, WorkloadStatus
from OpenBench.models import Result, SPSARun, Test, WorkloadSnapshot

TRINOMIAL_FIELDS   = ('losses', 'draws', 'wins')
PENTANOMIAL_FIELDS = ('LL', 'LD', 'DD', 'DW', 'WW')

def outcomes_of(source: Any, use_penta: bool) -> Outcomes:
    return Outcomes(
        trinomial   = (source.losses, source.draws, source.wins),
        pentanomial = (source.LL, source.LD, source.DD, source.DW, source.WW),
        use_penta   = use_penta,
    )

def outcomes_of_row(row: dict[str, Any], use_penta: bool) -> Outcomes:
    losses, draws, wins = (row[field] for field in TRINOMIAL_FIELDS)
    LL, LD, DD, DW, WW  = (row[field] for field in PENTANOMIAL_FIELDS)
    return Outcomes((losses, draws, wins), (LL, LD, DD, DW, WW), use_penta)

def machine_name(info: dict[str, Any]) -> str | None:
    name = info.get('machine_name')
    return name if isinstance(name, str) and name not in ('', 'None') else None

def uses_penta(test: Test) -> bool:
    return not test.use_tri

def reached_target(games: int, target: int | None) -> bool:
    return target is not None and target > 0 and games >= target

def workload_status(test: Test, target: int | None) -> WorkloadStatus:
    flags = (
        (test.deleted,                                         WorkloadStatus.DELETED),
        (test.passed,                                          WorkloadStatus.PASSED),
        (test.failed,                                          WorkloadStatus.FAILED),
        (test.finished and reached_target(test.games, target), WorkloadStatus.COMPLETED),
        (test.finished,                                        WorkloadStatus.STOPPED),
        (not test.approved,                                    WorkloadStatus.PENDING),
    )
    return next((status for flag, status in flags if flag), WorkloadStatus.ACTIVE)

def target_games(test: Test, mode: WorkloadMode) -> int | None:

    if mode in (WorkloadMode.GAMES, WorkloadMode.DATAGEN):
        return test.max_games

    if mode == WorkloadMode.SPSA:
        run = SPSARun.objects.filter(tune=test).only('pairs_per', 'iterations').first()
        return None if run is None else 2 * run.pairs_per * run.iterations

    return None

def workload_facts(test: Test) -> WorkloadFacts:

    mode   = WorkloadMode(test.test_mode)
    sprt   = SprtBounds(test.elolower, test.eloupper, test.lowerllr, test.upperllr) if mode == WorkloadMode.SPRT else None
    target = target_games(test, mode)

    return WorkloadFacts(
        id           = test.id,
        mode         = mode,
        status       = workload_status(test, target),
        created_at   = test.creation,
        updated_at   = test.updated,
        finished     = test.finished,
        outcomes     = outcomes_of(test, uses_penta(test)),
        llr          = test.currentllr,
        sprt         = sprt,
        target_games = target,
    )

def snapshot_points(test: Test) -> list[ProgressPoint]:
    use_penta = uses_penta(test)
    return [
        ProgressPoint(snapshot.created, snapshot.games, outcomes_of(snapshot, use_penta), snapshot.llr)
        for snapshot in WorkloadSnapshot.objects.filter(test=test).order_by('created', 'id')
    ]

def result_rows(test: Test) -> list[ResultRow]:

    use_penta = uses_penta(test)
    rows = Result.objects.filter(test=test).order_by('id').values(
        'machine_id', 'machine__user__username', 'machine__info', *TRINOMIAL_FIELDS, *PENTANOMIAL_FIELDS)

    return [
        ResultRow(
            machine_id   = row['machine_id'],
            machine_name = machine_name(row['machine__info'] or {}),
            owner        = row['machine__user__username'],
            cpu_name     = (row['machine__info'] or {}).get('cpu_name'),
            outcomes     = outcomes_of_row(row, use_penta),
        )
        for row in rows
    ]
