from collections.abc import Iterable
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
from OpenBench.diagnosis.sources import latest_errors, load_activities, load_fleet, worker_errors
from OpenBench.diagnosis.standing import FleetJudge
from OpenBench.insights.domain import WorkloadStatus
from OpenBench.models import Test


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


def diagnose_settled(workload: Test, now: datetime, with_errors: bool) -> Diagnosis | None:

    if workload.finished or workload.deleted:
        stopped = settled_status(workload) == WorkloadStatus.STOPPED
        errors = latest_errors(workload) if with_errors and stopped else ()
        return diagnose_finished(workload, Activity(errors=errors), now)
    if not workload.approved:
        return diagnose_pending()
    return None


def diagnose_workload(workload: Test, now: datetime | None = None) -> Diagnosis:
    now = now or timezone.now()
    settled = diagnose_settled(workload, now, with_errors=True)
    return settled or diagnose_active_workloads(now).get(workload.id) or diagnose_unknown()


def diagnose_workloads(workloads: Iterable[Test], now: datetime | None = None) -> dict[int, Diagnosis]:

    now = now or timezone.now()
    settled = {workload.id: diagnose_settled(workload, now, with_errors=False) for workload in workloads}
    active = diagnose_active_workloads(now) if any(found is None for found in settled.values()) else {}
    return {
        workload_id: found or active.get(workload_id) or diagnose_unknown() for workload_id, found in settled.items()
    }
