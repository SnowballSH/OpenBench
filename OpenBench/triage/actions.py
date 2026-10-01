from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from itertools import groupby

from django.db.models import QuerySet

from OpenBench.fleet.status import relative_age
from OpenBench.models import LogEvent
from OpenBench.models import Test as Workload
from OpenBench.triage.groups import load_workloads


@dataclass(frozen=True, slots=True)
class ActionRow:
    event: LogEvent
    workload: Workload | None
    count: int
    first_at: datetime
    ago: str


def operator_events() -> QuerySet[LogEvent]:
    return LogEvent.objects.filter(machine_id=0).order_by('-id')


def repeat_key(event: LogEvent) -> tuple[str, int, str]:
    return event.author, event.test_id, event.summary


def action_rows(events: Sequence[LogEvent], now: datetime) -> list[ActionRow]:

    workloads = load_workloads(event.test_id for event in events)
    runs = [list(run) for _, run in groupby(events, key=repeat_key)]
    return [
        ActionRow(
            event=run[0],
            workload=workloads.get(run[0].test_id),
            count=len(run),
            first_at=run[-1].created,
            ago=relative_age(now - run[0].created),
        )
        for run in runs
    ]
