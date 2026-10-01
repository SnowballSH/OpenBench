from collections.abc import Iterable, Mapping
from datetime import datetime

from django.utils import timezone

from OpenBench.diagnosis import scheduler
from OpenBench.diagnosis.domain import Activity, Diagnosis
from OpenBench.diagnosis.reasoning import (
    diagnose,
    diagnose_finished,
    diagnose_pending,
    diagnose_unknown,
    settled_status,
)
from OpenBench.diagnosis.sources import load_activities, load_fleet, stop_causes, worker_errors
from OpenBench.diagnosis.standing import FleetJudge
from OpenBench.insights.domain import WorkloadStatus
from OpenBench.models import LogEvent, Test


def diagnose_active_workloads(now: datetime) -> dict[int, Diagnosis]:

    # Every active Workload is judged from one shared picture of the fleet, so the cost is a fixed
    # number of queries however many Workloads and Machines there are
    active = scheduler.active_workloads()
    if not active:
        return {}

    ids = [workload.id for workload in active]
    errors = worker_errors(ids)
    judge = FleetJudge(load_fleet(active, errors, now))
    activities = load_activities(ids, errors, now)

    return {workload.id: diagnose(workload, judge, activities[workload.id], now) for workload in active}


def is_stopped(workload: Test) -> bool:
    return workload.finished and settled_status(workload) == WorkloadStatus.STOPPED


def diagnose_settled(workload: Test, now: datetime, causes: Mapping[int, LogEvent]) -> Diagnosis | None:

    if workload.finished or workload.deleted:
        return diagnose_finished(workload, Activity(stopped_by=causes.get(workload.id)), now)
    if not workload.approved:
        return diagnose_pending()
    return None


def diagnose_workloads(workloads: Iterable[Test], now: datetime | None = None) -> dict[int, Diagnosis]:

    now = now or timezone.now()
    workloads = list(workloads)
    stopped = [workload.id for workload in workloads if is_stopped(workload)]
    causes = stop_causes(stopped) if stopped else {}

    settled = {workload.id: diagnose_settled(workload, now, causes) for workload in workloads}
    active = diagnose_active_workloads(now) if any(found is None for found in settled.values()) else {}
    return {
        workload_id: found or active.get(workload_id) or diagnose_unknown() for workload_id, found in settled.items()
    }


def diagnose_workload(workload: Test, now: datetime | None = None) -> Diagnosis:
    return diagnose_workloads([workload], now)[workload.id]
