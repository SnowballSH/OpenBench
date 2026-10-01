from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

from OpenBench.fleet.sessions import ACTIVE_MACHINE
from OpenBench.models import EngineConfig, LogEvent, Machine, Test

ASSIGNMENT_WINDOW = ACTIVE_MACHINE
RECENT_WINDOW = timedelta(minutes=10)
FLEET_WINDOW = timedelta(hours=24)
BUILD_ALLOWANCE = timedelta(minutes=30)
STALL_FLOOR = timedelta(minutes=10)
STALL_FACTOR = 4
SHOWN_ERRORS = 3
BUILD_FAILURE_SUFFIX = 'build failed'
SHOWN_RIVALS = 3
SHOWN_GROUPS = 6


class DiagnosisState(StrEnum):
    AWAITING_APPROVAL = 'awaiting_approval'
    FINISHED = 'finished'
    STOPPED_BY_ERROR = 'stopped_by_error'
    RUNNING = 'running'
    STARTING = 'starting'
    STALLED = 'stalled'
    NO_WORKERS = 'no_workers'
    NO_ELIGIBLE_WORKERS = 'no_eligible_workers'
    OUTRANKED = 'outranked'
    LOW_SHARE = 'low_share'
    FAILING = 'failing'
    WAITING = 'waiting'
    UNKNOWN = 'unknown'


class Severity(StrEnum):
    OK = 'ok'
    INFO = 'info'
    WARNING = 'warning'


SEVERITY: dict[DiagnosisState, Severity] = {
    DiagnosisState.AWAITING_APPROVAL: Severity.INFO,
    DiagnosisState.FINISHED: Severity.OK,
    DiagnosisState.STOPPED_BY_ERROR: Severity.WARNING,
    DiagnosisState.RUNNING: Severity.OK,
    DiagnosisState.STARTING: Severity.INFO,
    DiagnosisState.STALLED: Severity.WARNING,
    DiagnosisState.NO_WORKERS: Severity.WARNING,
    DiagnosisState.NO_ELIGIBLE_WORKERS: Severity.WARNING,
    DiagnosisState.OUTRANKED: Severity.WARNING,
    DiagnosisState.LOW_SHARE: Severity.WARNING,
    DiagnosisState.FAILING: Severity.WARNING,
    DiagnosisState.WAITING: Severity.INFO,
    DiagnosisState.UNKNOWN: Severity.INFO,
}


class EvidenceKind(StrEnum):
    WORKERS = 'workers'
    LAST_RESULT = 'last_result'
    ASSIGNMENT = 'assignment'
    LAST_WORKER = 'last_worker'
    ELIGIBLE = 'eligible'
    INELIGIBLE = 'ineligible'
    HIGHER_PRIORITY = 'higher_priority'
    FOCUS = 'focus'
    THROUGHPUT_SHARE = 'throughput_share'
    ERROR = 'error'
    LIMIT = 'limit'


class ObstacleKind(StrEnum):
    ENGINE_UNSUPPORTED = 'engine_unsupported'
    ONLY_EXCLUDES = 'only_excludes'
    BLACKLISTED = 'blacklisted'
    SYZYGY = 'syzygy'
    NOISY = 'noisy'
    THREADS = 'threads'
    UNREADABLE = 'unreadable'


def is_build_failure(event: LogEvent) -> bool:
    # The summary Client/worker.py reports before it blacklists the workload for its session
    return event.summary.endswith(BUILD_FAILURE_SUFFIX)


@dataclass(frozen=True, slots=True)
class Link:
    href: str
    label: str


@dataclass(frozen=True, slots=True)
class Evidence:
    kind: EvidenceKind
    text: str
    link: Link | None = None


@dataclass(frozen=True, slots=True)
class Diagnosis:
    state: DiagnosisState
    severity: Severity
    headline: str
    brief: str
    evidence: list[Evidence] = field(default_factory=list)

    @property
    def calm(self) -> bool:
        return self.severity != Severity.WARNING

    @property
    def shown(self) -> bool:
        return self.state != DiagnosisState.FINISHED


@dataclass(frozen=True, slots=True)
class Obstacle:
    kind: ObstacleKind
    detail: str


@dataclass(frozen=True, slots=True)
class WorkerGroup:
    label: str
    machine: Machine
    hosts: int
    last_seen: datetime

    def recent(self, now: datetime) -> bool:
        return now - self.last_seen <= RECENT_WINDOW


@dataclass(frozen=True, slots=True)
class Preparing:
    machine_id: int
    assigned_at: datetime


@dataclass(frozen=True, slots=True)
class Activity:
    last_result_at: datetime | None = None
    last_assigned_at: datetime | None = None
    preparing: tuple[Preparing, ...] = ()
    errors: tuple[LogEvent, ...] = ()
    stopped_by: LogEvent | None = None


@dataclass(frozen=True, slots=True)
class Fleet:
    active: tuple[Test, ...]
    groups: tuple[WorkerGroup, ...]
    assigned: tuple[Machine, ...]
    last_seen: Machine | None
    configs: Mapping[str, EngineConfig] = field(default_factory=dict)
    build_failures: frozenset[tuple[int, int]] = frozenset()
