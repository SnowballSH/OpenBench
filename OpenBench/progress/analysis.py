import math
from collections import defaultdict
from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime, time, timedelta
from itertools import groupby

from OpenBench.insights.strength import EloInterval, elo_interval
from OpenBench.progress.domain import (
    DEFAULT_WINDOW,
    ENGINE_NAME_LIMIT,
    NO_OUTCOMES,
    TOP_LIMIT,
    Author,
    Contributor,
    DailyGames,
    DayMaximum,
    GreenRow,
    GreenTest,
    OutcomeCounts,
    Scope,
    Summary,
    WeeklyOutcomes,
    Window,
)


def parse_window(raw: str | None) -> Window | None:
    if raw is None or raw == "":
        return DEFAULT_WINDOW
    try:
        return Window(raw.strip().lower())
    except ValueError:
        return None


def parse_engine(raw: str | None) -> str | None:
    name = (raw or "").strip()
    return name[:ENGINE_NAME_LIMIT] or None


def utc_midnight(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=UTC)


def utc_day(moment: datetime) -> date:
    return moment.astimezone(UTC).date()


def make_scope(window: Window, engine: str | None, now: datetime) -> Scope:
    today = utc_day(now)
    days = window.days
    since = None if days is None else utc_midnight(today - timedelta(days=days - 1))
    return Scope(window=window, engine=engine, since=since, today=today)


def week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())


def finite_value(interval: EloInterval | None) -> float | None:
    if interval is None or not math.isfinite(interval.value):
        return None
    return interval.value


def green_tests(rows: Iterable[GreenRow]) -> list[GreenTest]:
    greens: list[GreenTest] = []
    total = 0.0
    for row in sorted(rows, key=lambda row: (row.finished_at, row.id)):
        interval = elo_interval(row.outcomes.primary())
        total += finite_value(interval) or 0.0
        greens.append(
            GreenTest(
                id=row.id,
                name=row.name,
                finished_at=row.finished_at,
                games=row.games,
                elo_bounds=row.elo_bounds,
                elo=interval,
                cumulative_elo=total,
            )
        )
    return greens


def games_by_day(
    maxima: Iterable[DayMaximum], baselines: Mapping[int, int]
) -> dict[date, int]:
    totals: defaultdict[date, int] = defaultdict(int)
    ordered = sorted(maxima, key=lambda row: (row.test_id, row.day))
    for test_id, rows in groupby(ordered, key=lambda row: row.test_id):
        previous = baselines.get(test_id, 0)
        for row in rows:
            totals[row.day] += max(0, row.games - previous)
            previous = max(previous, row.games)
    return dict(totals)


def days_between(start: date, end: date) -> list[date]:
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]


def daily_series(
    totals: Mapping[date, int], start: date, end: date
) -> list[DailyGames]:
    return [DailyGames(day, totals.get(day, 0)) for day in days_between(start, end)]


def weekly_series(
    counts: Mapping[date, OutcomeCounts], start: date, end: date
) -> list[WeeklyOutcomes]:
    weeks = days_between(week_start(start), week_start(end))[::7]
    return [
        WeeklyOutcomes(week, found.passed, found.failed, found.stopped)
        for week in weeks
        for found in [counts.get(week, NO_OUTCOMES)]
    ]


def series_start(scope: Scope, earliest: Iterable[date | None]) -> date:
    if scope.since is not None:
        return scope.since.date()
    return min((day for day in earliest if day is not None), default=scope.today)


def share(part: int, whole: int) -> float | None:
    return part / whole if whole else None


def top_contributors(games_by_user: Mapping[str, int]) -> list[Contributor]:
    total = sum(games_by_user.values())
    ranked = sorted(games_by_user.items(), key=lambda item: (-item[1], item[0]))
    return [
        Contributor(username, games, share(games, total))
        for username, games in ranked[:TOP_LIMIT]
    ]


def top_authors(tests_by_author: Mapping[str, int]) -> list[Author]:
    total = sum(tests_by_author.values())
    ranked = sorted(tests_by_author.items(), key=lambda item: (-item[1], item[0]))
    return [
        Author(username, tests, share(tests, total))
        for username, tests in ranked[:TOP_LIMIT]
    ]


def summarize(
    greens: list[GreenTest],
    outcomes: Iterable[OutcomeCounts],
    daily: list[DailyGames],
    tests_by_author: Mapping[str, int],
    games_by_user: Mapping[str, int],
) -> Summary:
    sprt = sum(outcomes, NO_OUTCOMES)
    games = sum(day.games for day in daily)
    return Summary(
        elo_gained=greens[-1].cumulative_elo if greens else 0.0,
        greens=len(greens),
        greens_without_elo=sum(finite_value(green.elo) is None for green in greens),
        sprt=sprt,
        sprt_pass_rate=sprt.pass_rate,
        games=games,
        games_per_day=games / len(daily) if daily else None,
        days=len(daily),
        tests_created=sum(tests_by_author.values()),
        authors=len(tests_by_author),
        contributors=sum(games > 0 for games in games_by_user.values()),
    )
