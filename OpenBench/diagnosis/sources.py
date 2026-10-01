from collections.abc import Iterable, Sequence
from datetime import datetime

from django.db.models import F, Max, Q

from OpenBench.diagnosis.domain import (
    ASSIGNMENT_WINDOW,
    BUILD_ALLOWANCE,
    Activity,
    Fleet,
    Preparing,
    is_build_failure,
)
from OpenBench.diagnosis.fleet import group_workers
from OpenBench.fleet.housekeeping import exits_when_idle
from OpenBench.machine_info import text_of
from OpenBench.models import EngineConfig, LogEvent, Machine, Result, Test

FLEET_SAMPLE = 120
ERROR_SAMPLE = 200


def newest_machines() -> list[Machine]:
    return list(Machine.objects.select_related('user').order_by('-updated', '-id')[:FLEET_SAMPLE])


def worker_errors(workload_ids: Iterable[int]) -> list[LogEvent]:
    events = LogEvent.objects.filter(test_id__in=list(workload_ids), machine_id__gt=0)
    return list(events.order_by('-id')[:ERROR_SAMPLE])


def session_blacklists(errors: Iterable[LogEvent], machines: Iterable[Machine]) -> frozenset[tuple[int, int]]:
    # A Client that exits when idle takes its blacklist with it: the job that replaces it starts with none
    lasting = {machine.id for machine in machines if not exits_when_idle(text_of(machine.info, 'cli_options'))}
    return frozenset(
        (event.test_id, event.machine_id) for event in errors if is_build_failure(event) and event.machine_id in lasting
    )


def load_fleet(active: Sequence[Test], errors: Sequence[LogEvent], now: datetime) -> Fleet:

    machines = newest_machines()
    failures = session_blacklists(errors, machines)

    return Fleet(
        active=tuple(active),
        groups=tuple(group_workers(machines, now, {machine_id for _, machine_id in failures})),
        assigned=tuple(machine for machine in machines if now - machine.updated <= ASSIGNMENT_WINDOW),
        last_seen=machines[0] if machines else None,
        configs=EngineConfig.objects.in_bulk(field_name='name'),
        build_failures=failures,
    )


def load_result_times(workload_ids: Sequence[int]) -> dict[int, tuple[datetime | None, datetime | None]]:
    rows = (
        Result.objects.filter(test_id__in=workload_ids)
        .order_by()
        .values('test_id')
        .annotate(reported=Max('updated', filter=Q(games__gt=0)), assigned=Max('updated', filter=Q(games=0)))
    )
    return {row['test_id']: (row['reported'], row['assigned']) for row in rows}


def load_preparing(workload_ids: Sequence[int], now: datetime) -> dict[int, list[Preparing]]:

    # A Result with no games, on a Machine still pointed at the Workload, is a worker that has yet to report
    rows = Result.objects.filter(
        test_id__in=workload_ids, games=0, updated__gte=now - BUILD_ALLOWANCE, machine__workload=F('test_id')
    ).values_list('test_id', 'machine_id', 'updated')

    preparing: dict[int, list[Preparing]] = {}
    for test_id, machine_id, assigned_at in rows.order_by('-updated', '-id'):
        preparing.setdefault(test_id, []).append(Preparing(machine_id, assigned_at))
    return preparing


def load_activities(workload_ids: Sequence[int], errors: Sequence[LogEvent], now: datetime) -> dict[int, Activity]:

    times = load_result_times(workload_ids)
    preparing = load_preparing(workload_ids, now)

    return {
        workload_id: Activity(
            last_result_at=times.get(workload_id, (None, None))[0],
            last_assigned_at=times.get(workload_id, (None, None))[1],
            preparing=tuple(preparing.get(workload_id, ())),
            errors=tuple(event for event in errors if event.test_id == workload_id),
        )
        for workload_id in workload_ids
    }


def stop_causes(workload_ids: Sequence[int]) -> dict[int, LogEvent]:

    # clientBenchError finishes the Test and logs the mismatch without a log file. It is the cause of
    # the stop only while it is the Workload's newest event: a later action logs one of its own
    newest = LogEvent.objects.filter(test_id__in=workload_ids).order_by().values('test_id').annotate(newest=Max('id'))
    causes = LogEvent.objects.filter(id__in=newest.values('newest'), machine_id__gt=0, log_file='')
    return {event.test_id: event for event in causes}
