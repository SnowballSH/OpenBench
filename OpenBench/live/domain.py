from dataclasses import dataclass
from datetime import timedelta

from OpenBench.diagnosis.domain import Diagnosis, Severity
from OpenBench.insights.domain import WorkloadStatus
from OpenBench.insights.listing import RowTimingKind
from OpenBench.listing_rows import RowMoment

FRESHNESS = timedelta(minutes=1)
RUNNING_STATUSES = frozenset({WorkloadStatus.PENDING, WorkloadStatus.ACTIVE})


@dataclass(frozen=True, slots=True)
class LiveProgress:
    kind: str
    fraction: float
    label: str


@dataclass(frozen=True, slots=True)
class LiveTiming:
    kind: RowTimingKind
    text: str | None
    estimate: bool
    note: str | None
    rate: str | None
    rate_window: str | None


@dataclass(frozen=True, slots=True)
class LiveReason:
    severity: Severity
    headline: str
    brief: str


@dataclass(frozen=True, slots=True)
class LiveResult:
    status: WorkloadStatus
    games: int
    colour: str
    outcome: str
    statblock: list[str]


@dataclass(frozen=True, slots=True)
class LiveRow:
    id: int
    result: LiveResult
    progress: LiveProgress | None
    timing: LiveTiming | None
    reason: LiveReason | None
    moment: RowMoment | None


@dataclass(frozen=True, slots=True)
class LiveListing:
    token: str
    machine_status: str
    rows: list[LiveRow]


@dataclass(frozen=True, slots=True)
class LiveWorkload:
    token: str
    id: int
    result: LiveResult
    diagnosis: Diagnosis | None


@dataclass(frozen=True, slots=True)
class Unchanged:
    token: str
