from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from OpenBench.fleet.status import relative_age
from OpenBench.models import LogEvent
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
    ago: str


def error_rows(events: Sequence[LogEvent], now: datetime) -> list[ErrorRow]:
    workloads = load_workloads(event.test_id for event in events)
    return [
        ErrorRow(
            event=event,
            signature=cached_signature(event.summary),
            workload=workloads.get(event.test_id),
            has_log=has_log(event),
            ago=relative_age(now - event.created),
        )
        for event in events
    ]
