from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum

from OpenBench.insights.domain import Outcomes
from OpenBench.insights.strength import EloInterval

TOP_LIMIT = 10
ENGINE_NAME_LIMIT = 64


class Window(StrEnum):
    DAYS_30 = "30d"
    DAYS_90 = "90d"
    YEAR = "1y"
    ALL = "all"

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
    Window.DAYS_30: "30 days",
    Window.DAYS_90: "90 days",
    Window.YEAR: "1 year",
    Window.ALL: "All time",
}

DEFAULT_WINDOW = Window.DAYS_90


@dataclass(frozen=True, slots=True)
class Scope:
    window: Window
    engine: str | None
    since: datetime | None
    today: date


@dataclass(frozen=True, slots=True)
class GreenRow:
    id: int
    name: str
    finished_at: datetime
    games: int
    elo_bounds: tuple[float, float]
    outcomes: Outcomes


@dataclass(frozen=True, slots=True)
class GreenTest:
    id: int
    name: str
    finished_at: datetime
    games: int
    elo_bounds: tuple[float, float]
    elo: EloInterval | None
    cumulative_elo: float


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

    def __add__(self, other: "OutcomeCounts") -> "OutcomeCounts":
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
    elo_gained: float
    greens: int
    greens_without_elo: int
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
    greens: list[GreenTest]
    weekly_outcomes: list[WeeklyOutcomes]
    daily_games: list[DailyGames]
    top_contributors: list[Contributor]
    top_authors: list[Author]
