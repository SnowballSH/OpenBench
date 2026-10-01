from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime

from OpenBench.fleet.status import relative_age
from OpenBench.models import LogEvent, Machine
from OpenBench.models import Test as Workload
from OpenBench.triage.groups import cached_signature, load_workloads
from OpenBench.triage.kinds import Signature
from OpenBench.triage.logs import has_log


@dataclass(frozen=True, slots=True)
class ErrorRow:
    event: LogEvent
    signature: Signature
    workload: Workload | None
    has_log: bool
    registered: bool
    ago: str


def error_rows(events: Sequence[LogEvent], now: datetime) -> list[ErrorRow]:
    workloads = load_workloads(event.test_id for event in events)
    registered = registered_machines(event.machine_id for event in events)
    return [
        ErrorRow(
            event=event,
            signature=cached_signature(event.summary),
            workload=workloads.get(event.test_id),
            has_log=has_log(event),
            registered=event.machine_id in registered,
            ago=relative_age(now - event.created),
        )
        for event in events
    ]


def registered_machines(machine_ids: Iterable[int]) -> frozenset[int]:
    return frozenset(Machine.objects.filter(id__in=set(machine_ids)).values_list('id', flat=True))
