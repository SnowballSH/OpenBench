from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from OpenBench.diagnosis.domain import DiagnosisState, Severity
from OpenBench.insights.domain import WorkloadMode, WorkloadStatus
from OpenBench.insights.eta import Eta
from OpenBench.insights.strength import EloInterval
from OpenBench.progress.domain import Commit, RunStatus, TimeClass
from OpenBench.triage.domain import GroupRow

MAX_WINDOW = timedelta(days=30)
FINISHED_SENT = 100
RUNNING_SENT = 100
ERRORS_SENT = 20
POOLS_SENT = 8
MOVES_SENT = 50
MOST_BUCKETS = 72


class Preset(StrEnum):
    HOURS_8 = '8h'
    HOURS_24 = '24h'
    DAYS_3 = '3d'
    DAYS_7 = '7d'

    @property
    def span(self) -> timedelta:
        return PRESET_SPANS[self]

    @property
    def label(self) -> str:
        return PRESET_LABELS[self]


PRESET_SPANS: dict[Preset, timedelta] = {
    Preset.HOURS_8: timedelta(hours=8),
    Preset.HOURS_24: timedelta(hours=24),
    Preset.DAYS_3: timedelta(days=3),
    Preset.DAYS_7: timedelta(days=7),
}

PRESET_LABELS: dict[Preset, str] = {
    Preset.HOURS_8: '8 hours',
    Preset.HOURS_24: '24 hours',
    Preset.DAYS_3: '3 days',
    Preset.DAYS_7: '7 days',
}

DEFAULT_PRESET = Preset.HOURS_24


@dataclass(frozen=True, slots=True)
class WindowChoice:
    preset: Preset | None
    since: datetime | None


DEFAULT_CHOICE = WindowChoice(DEFAULT_PRESET, None)


@dataclass(frozen=True, slots=True)
class DigestWindow:
    preset: Preset | None
    since: datetime
    until: datetime
    clamped: bool

    @property
    def span(self) -> timedelta:
        return self.until - self.since

    def holds(self, moment: datetime | None) -> bool:
        return moment is not None and self.since <= moment <= self.until


@dataclass(frozen=True, slots=True)
class WorkloadRef:
    id: int
    url: str
    title: str
    commits: str | None
    author: str
    engine: str
    mode: WorkloadMode
    time_control: str
    time_class: TimeClass


@dataclass(frozen=True, slots=True)
class FinishedWorkload:
    workload: WorkloadRef
    status: WorkloadStatus
    elo: EloInterval | None
    games: int
    started_at: datetime
    finished_at: datetime
    duration_seconds: float
    stop_reason: str | None


@dataclass(frozen=True, slots=True)
class FinishedCounts:
    total: int
    passed: int
    failed: int

    @property
    def undecided(self) -> int:
        return self.total - self.passed - self.failed


@dataclass(frozen=True, slots=True)
class FinishedDigest:
    counts: FinishedCounts
    workloads: list[FinishedWorkload]
    omitted: int


@dataclass(frozen=True, slots=True)
class DiagnosisBrief:
    state: DiagnosisState
    severity: Severity
    headline: str
    brief: str


@dataclass(frozen=True, slots=True)
class LlrProgress:
    value: float
    lower: float
    upper: float


@dataclass(frozen=True, slots=True)
class RunningWorkload:
    workload: WorkloadRef
    status: WorkloadStatus
    started_in_window: bool
    started_at: datetime
    games: int
    elo: EloInterval | None
    llr: LlrProgress | None
    games_per_hour: float | None
    eta: Eta | None
    diagnosis: DiagnosisBrief


@dataclass(frozen=True, slots=True)
class RunningDigest:
    total: int
    pending: int
    started: int
    workloads: list[RunningWorkload]
    omitted: int


@dataclass(frozen=True, slots=True)
class TrunkMove:
    index: int
    base: Commit
    dev: Commit
    repo: str
    subject: str
    verdict: RunStatus
    provisional: bool
    elo: EloInterval | None
    games: int
    measured_at: datetime | None
    runs: list[int]


@dataclass(frozen=True, slots=True)
class ClassMovement:
    time_class: TimeClass
    moves: list[TrunkMove]
    moves_omitted: int
    measured: int
    accepted: int
    provisional: int
    net: EloInterval | None


@dataclass(frozen=True, slots=True)
class TrunkMovement:
    engine: str
    head: Commit
    trunk_length: int
    classes: list[ClassMovement]


@dataclass(frozen=True, slots=True)
class GamesBucket:
    start: datetime
    games: list[int]

    @property
    def total(self) -> int:
        return sum(self.games)


@dataclass(frozen=True, slots=True)
class PoolActivity:
    owner: str
    label: str
    cpu_name: str
    hosts: int


@dataclass(frozen=True, slots=True)
class FleetActivity:
    games: int
    core_hours: float | None
    core_hours_estimated: bool
    games_without_hours: int
    hosts: int
    pools: list[PoolActivity]
    pools_omitted: int
    bucket_hours: int
    classes: list[TimeClass]
    buckets: list[GamesBucket]
    peak_games_per_hour: float | None
    peak_at: datetime | None


@dataclass(frozen=True, slots=True)
class ErrorLine:
    row: GroupRow
    new: bool


@dataclass(frozen=True, slots=True)
class ErrorDigest:
    total: int
    new: int
    unresolved: int
    lines: list[ErrorLine]
    omitted: int
    truncated: bool


@dataclass(frozen=True, slots=True)
class DigestReport:
    generated_at: datetime
    window: DigestWindow
    headline: list[str]
    finished: FinishedDigest
    running: RunningDigest
    trunk: list[TrunkMovement]
    fleet: FleetActivity
    errors: ErrorDigest
