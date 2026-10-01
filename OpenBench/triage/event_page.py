from dataclasses import dataclass
from datetime import datetime

from OpenBench.fleet.pools import pool_label, short_name
from OpenBench.fleet.status import relative_age
from OpenBench.machine_info import int_of, text_of
from OpenBench.models import LogEvent, Machine
from OpenBench.models import Test as Workload
from OpenBench.triage.domain import BenchMismatch
from OpenBench.triage.groups import bench_mismatch, load_workloads
from OpenBench.triage.kinds import Signature, signature
from OpenBench.triage.logs import KeyLine, LogExcerpt, excerpt, has_log, key_lines, log_lines, log_path, read_log

MAX_COMPILERS = 8
COMPILER_TEXT_LENGTH = 64


@dataclass(frozen=True, slots=True)
class Reporter:
    machine_id: int
    owner: str
    name: str | None
    pool: str | None
    cpu_name: str | None
    os_name: str | None
    threads: int
    compilers: tuple[str, ...]
    known: bool


@dataclass(frozen=True, slots=True)
class LogView:
    size: int
    limit: int
    truncated: bool
    excerpt: LogExcerpt
    key_lines: tuple[KeyLine, ...]


@dataclass(frozen=True, slots=True)
class EventDetail:
    event: LogEvent
    signature: Signature
    workload: Workload | None
    reporter: Reporter
    occurrences: int
    bench: BenchMismatch | None
    ago: str
    expects_log: bool
    log: LogView | None


def compiler_lines(info: object) -> tuple[str, ...]:

    compilers = info.get('compilers') if isinstance(info, dict) else None
    if not isinstance(compilers, dict):
        return ()

    lines = [
        f'{engine}: {" ".join(str(part) for part in detail)}'[:COMPILER_TEXT_LENGTH]
        for engine, detail in compilers.items()
        if isinstance(detail, list | tuple)
    ]
    return tuple(lines[:MAX_COMPILERS])


def os_label(info: object) -> str | None:
    name = text_of(info, 'os_name')
    return ' '.join(filter(None, (name, text_of(info, 'os_ver')))) if name else None


def reporter(event: LogEvent) -> Reporter:

    machine = Machine.objects.filter(id=event.machine_id).first()
    if machine is None:
        return Reporter(event.machine_id, event.author, None, None, None, None, 0, (), known=False)

    name, cpu_name = text_of(machine.info, 'machine_name'), text_of(machine.info, 'cpu_name')
    return Reporter(
        machine_id=machine.id,
        owner=event.author,
        name=short_name(name) if name else None,
        pool=pool_label(name, cpu_name),
        cpu_name=cpu_name,
        os_name=os_label(machine.info),
        threads=int_of(machine.info, 'concurrency'),
        compilers=compiler_lines(machine.info),
        known=True,
    )


def log_view(event: LogEvent) -> LogView | None:

    if (path := log_path(event)) is None:
        return None

    log = read_log(path)
    lines = log_lines(log.text)
    shown = excerpt(lines)
    return LogView(
        size=log.size,
        limit=log.limit,
        truncated=log.truncated,
        excerpt=shown,
        key_lines=key_lines(lines, shown.shown_numbers),
    )


def event_detail(event: LogEvent, now: datetime) -> EventDetail:

    found = signature(event.summary)
    workload = load_workloads([event.test_id]).get(event.test_id)
    return EventDetail(
        event=event,
        signature=found,
        workload=workload,
        reporter=reporter(event),
        occurrences=LogEvent.objects.filter(test_id=event.test_id, summary=event.summary, machine_id__gt=0).count(),
        bench=bench_mismatch(found, () if found.bench is None else (found.bench,), workload),
        ago=relative_age(now - event.created),
        expects_log=has_log(event),
        log=log_view(event),
    )
