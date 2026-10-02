from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum

from OpenBench.insights.domain import Outcomes
from OpenBench.insights.strength import EloInterval
from OpenBench.releases.domain import ReleaseAnchor

TOP_LIMIT = 10
ENGINE_NAME_LIMIT = 64
TRUNK_STEPS_SENT = 500
CANDIDATES_SENT = 50
DETACHED_SENT = 50
ANCHOR_POINTS_SENT = 200
ANCHOR_BRANCHES_SENT = 50
MINIMUM_SAMPLE = 30


class Window(StrEnum):
    DAYS_30 = '30d'
    DAYS_90 = '90d'
    YEAR = '1y'
    ALL = 'all'

    @property
    def days(self) -> int | None:
        return WINDOW_DAYS[self]

    @property
    def label(self) -> str:
        return WINDOW_LABELS[self]


WINDOW_DAYS: dict[Window, int | None] = {
    Window.DAYS_30: 30,
    Window.DAYS_90: 90,
    Window.YEAR: 365,
    Window.ALL: None,
}

WINDOW_LABELS: dict[Window, str] = {
    Window.DAYS_30: '30 days',
    Window.DAYS_90: '90 days',
    Window.YEAR: '1 year',
    Window.ALL: 'All time',
}

DEFAULT_WINDOW = Window.DAYS_90


@dataclass(frozen=True, slots=True)
class Scope:
    window: Window
    engine: str | None
    since: datetime | None
    today: date


class TimeClass(StrEnum):
    STC = 'stc'
    LTC = 'ltc'
    VLTC = 'vltc'
    SMP = 'smp'
    OTHER = 'other'

    @property
    def label(self) -> str:
        return TIME_CLASS_LABELS[self]

    @property
    def chained(self) -> bool:
        return self is not TimeClass.OTHER


TIME_CLASS_LABELS: dict[TimeClass, str] = {
    TimeClass.STC: 'STC',
    TimeClass.LTC: 'LTC',
    TimeClass.VLTC: 'VLTC',
    TimeClass.SMP: 'SMP',
    TimeClass.OTHER: 'Other',
}


class RunMode(StrEnum):
    SPRT = 'SPRT'
    GAMES = 'GAMES'


class RunStatus(StrEnum):
    PASSED = 'passed'
    FAILED = 'failed'
    RUNNING = 'running'
    PENDING = 'pending'
    COMPLETED = 'completed'
    STOPPED = 'stopped'


class Pooling(StrEnum):
    PENTANOMIAL = 'pentanomial'
    TRINOMIAL = 'trinomial'


@dataclass(frozen=True, slots=True, order=True)
class Commit:
    sha: str
    network: str = ''


@dataclass(frozen=True, slots=True)
class HostCounters:
    host: str
    counted_games: int
    dev_nodes: int
    dev_ms: int
    base_nodes: int
    base_ms: int


@dataclass(frozen=True, slots=True)
class RunRow:
    id: int
    engine: str
    repo: str
    base: Commit
    dev: Commit
    subject: str
    author: str
    mode: RunMode
    status: RunStatus
    time_class: TimeClass
    time_control: str
    created_at: datetime
    finished_at: datetime | None
    games: int
    outcomes: Outcomes
    dev_threads: int = 1
    base_threads: int = 1
    started_at: datetime | None = None
    dev_bench: int = 0
    base_bench: int = 0
    hosts: tuple[HostCounters, ...] = ()
    dev_name: str = ''


@dataclass(frozen=True, slots=True)
class Run:
    id: int
    mode: RunMode
    status: RunStatus
    time_control: str
    created_at: datetime
    finished_at: datetime | None
    games: int
    elo: EloInterval | None
    started_at: datetime | None = None
    counted_games: int = 0
    core_hours: float | None = None


@dataclass(frozen=True, slots=True)
class SpeedRatio:
    ratio: float
    lower: float | None
    upper: float | None
    hosts: int
    games: int


@dataclass(frozen=True, slots=True)
class ClassSpeed:
    time_class: TimeClass
    speed: SpeedRatio


@dataclass(frozen=True, slots=True)
class StepSpeed:
    pooled: SpeedRatio
    classes: list[ClassSpeed]
    classes_differ: bool


@dataclass(frozen=True, slots=True)
class StepCost:
    runs: int
    games: int
    decision_seconds: float | None
    core_hours: float | None
    counted_games: int


NO_COST = StepCost(0, 0, None, None, 0)


@dataclass(frozen=True, slots=True)
class Measurement:
    time_class: TimeClass
    verdict: RunStatus
    games: int
    pooling: Pooling
    provisional: bool
    elo: EloInterval | None
    runs: list[Run]


@dataclass(frozen=True, slots=True)
class Step:
    base: Commit
    dev: Commit
    repo: str
    subject: str
    author: str
    first_run: int
    first_tested_at: datetime
    last_tested_at: datetime
    measured_at: datetime
    measurements: list[Measurement]
    base_bench: int | None = None
    dev_bench: int | None = None
    speed: StepSpeed | None = None
    cost: StepCost = NO_COST

    def measurement(self, time_class: TimeClass) -> Measurement | None:
        return next((found for found in self.measurements if found.time_class == time_class), None)


@dataclass(frozen=True, slots=True)
class Candidate:
    depth: int
    step: Step


@dataclass(frozen=True, slots=True)
class TrunkStep:
    index: int
    step: Step
    candidates: list[Candidate]
    candidates_omitted: int


@dataclass(frozen=True, slots=True)
class DirectRun:
    first_index: int
    last_index: int
    step: Step


@dataclass(frozen=True, slots=True)
class OtherLineage:
    root: Commit
    steps: int
    taken: int


@dataclass(frozen=True, slots=True)
class Lineage:
    root: Commit
    head: Commit
    trunk: list[Step]
    branches: dict[Commit, list[Candidate]]
    direct: list[DirectRun]
    detached: list[Step]
    others: list[OtherLineage]


@dataclass(frozen=True, slots=True)
class ChainPoint:
    index: int
    elo: EloInterval | None
    cumulative: EloInterval | None
    projected: EloInterval | None


@dataclass(frozen=True, slots=True)
class ChainSeries:
    time_class: TimeClass
    points: list[ChainPoint]
    total: EloInterval | None
    measured: int
    provisional: int
    steps: int


@dataclass(frozen=True, slots=True)
class DirectCheck:
    time_class: TimeClass
    base: Commit
    dev: Commit
    repo: str
    first_index: int
    last_index: int
    direct: Measurement
    chained: EloInterval | None
    measured: int
    steps: int


@dataclass(frozen=True, slots=True)
class LineageReport:
    engine: str
    classes: list[TimeClass]
    origin: Commit
    origin_candidates: list[Candidate]
    origin_candidates_omitted: int
    head: Commit
    trunk_length: int
    steps: list[TrunkStep]
    steps_omitted: int
    series: list[ChainSeries]
    direct: list[DirectCheck]
    detached: list[Step]
    detached_omitted: int
    others: list[OtherLineage]


@dataclass(frozen=True, slots=True)
class AnchorPoint:
    time_class: TimeClass
    dev: Commit
    repo: str
    subject: str
    committed_at: datetime | None
    measured_at: datetime
    first_run: int
    newer_bases: int
    measurement: Measurement


@dataclass(frozen=True, slots=True)
class AnchorSeries:
    time_class: TimeClass
    latest: AnchorPoint | None
    points: list[AnchorPoint]


@dataclass(frozen=True, slots=True)
class ReleaseReport:
    engine: str
    repo: str
    anchor: ReleaseAnchor | None
    series: list[AnchorSeries]
    branches: list[AnchorPoint]
    points_omitted: int = 0
    branches_omitted: int = 0


@dataclass(frozen=True, slots=True)
class RatioInterval:
    ratio: float
    lower: float | None
    upper: float | None


@dataclass(frozen=True, slots=True)
class SpeedPoint:
    index: int
    step: SpeedRatio | None
    cumulative: RatioInterval | None


@dataclass(frozen=True, slots=True)
class SpeedSeries:
    points: list[SpeedPoint]
    total: RatioInterval | None
    measured: int
    unbounded: int
    steps: int


@dataclass(frozen=True, slots=True)
class CostBucket:
    steps: int
    runs: int
    games: int
    core_hours: float
    counted_games: int
    estimated_core_hours: float | None
    games_share: float | None
    core_share: float | None


@dataclass(frozen=True, slots=True)
class ClassEconomics:
    time_class: TimeClass
    passed: int
    failed: int
    pass_rate: float | None
    median_games_to_pass: float | None
    median_games_to_fail: float | None
    games: int
    finished_games: int
    core_hours: float
    chained_elo: EloInterval | None
    games_per_elo: float | None


@dataclass(frozen=True, slots=True)
class WeeklySteps:
    week_start: date
    steps: int


@dataclass(frozen=True, slots=True)
class Cadence:
    weekly: list[WeeklySteps]
    joined: int
    span_days: int
    steps_per_week: float | None
    acceptance_samples: int
    median_acceptance_seconds: float | None
    mean_acceptance_seconds: float | None
    confirmation_samples: int
    median_confirmation_seconds: float | None


@dataclass(frozen=True, slots=True)
class Economics:
    trunk: CostBucket
    failed: CostBucket
    other: CostBucket
    counter_coverage: float | None
    core_hours_estimated: bool
    classes: list[ClassEconomics]
    speed: SpeedSeries
    cadence: Cadence


@dataclass(frozen=True, slots=True)
class LineageSummary:
    trunk_steps: int
    candidates: int
    measurements: int
    runs: int


NO_LINEAGE = LineageSummary(0, 0, 0, 0)


@dataclass(frozen=True, slots=True)
class OutcomeCounts:
    passed: int
    failed: int
    stopped: int

    @property
    def total(self) -> int:
        return self.passed + self.failed + self.stopped

    @property
    def pass_rate(self) -> float | None:
        decided = self.passed + self.failed
        return self.passed / decided if decided else None

    def __add__(self, other: OutcomeCounts) -> OutcomeCounts:
        return OutcomeCounts(
            self.passed + other.passed,
            self.failed + other.failed,
            self.stopped + other.stopped,
        )


NO_OUTCOMES = OutcomeCounts(0, 0, 0)


@dataclass(frozen=True, slots=True)
class WeeklyOutcomes:
    week_start: date
    passed: int
    failed: int
    stopped: int


@dataclass(frozen=True, slots=True)
class DayMaximum:
    test_id: int
    day: date
    games: int


@dataclass(frozen=True, slots=True)
class DailyGames:
    day: date
    games: int


@dataclass(frozen=True, slots=True)
class Contributor:
    username: str
    games: int
    share: float | None


@dataclass(frozen=True, slots=True)
class Author:
    username: str
    tests: int
    share: float | None


@dataclass(frozen=True, slots=True)
class Summary:
    lineage: LineageSummary
    sprt: OutcomeCounts
    sprt_pass_rate: float | None
    games: int
    games_per_day: float | None
    days: int
    tests_created: int
    authors: int
    contributors: int


@dataclass(frozen=True, slots=True)
class ProgressReport:
    generated_at: datetime
    engine: str | None
    window: Window
    start: date
    end: date
    summary: Summary
    release: ReleaseReport | None
    lineage: LineageReport | None
    economics: Economics | None
    lineage_engines: list[str]
    weekly_outcomes: list[WeeklyOutcomes]
    daily_games: list[DailyGames]
    top_contributors: list[Contributor]
    top_authors: list[Author]
