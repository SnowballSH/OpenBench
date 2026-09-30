from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from urllib.parse import quote, urlencode

from OpenBench.insights.strength import EloInterval
from OpenBench.progress.analysis import finite_value, utc_day
from OpenBench.progress.domain import (
    Author,
    Contributor,
    DailyGames,
    GreenTest,
    ProgressReport,
    Summary,
    WeeklyOutcomes,
    Window,
)

GREENS_LISTED = 100
DASH = "—"
MINUS = "−"


@dataclass(frozen=True, slots=True)
class Tile:
    label: str
    value: str
    meta: str
    tone: str | None = None


@dataclass(frozen=True, slots=True)
class WindowOption:
    label: str
    url: str
    current: bool


@dataclass(frozen=True, slots=True)
class GreenLine:
    url: str
    name: str
    finished_on: date
    elo: str
    bounds: str
    games: str
    cumulative: str


@dataclass(frozen=True, slots=True)
class ShareLine:
    name: str
    url: str | None
    value: str
    share: str
    percent: str


@dataclass(frozen=True, slots=True)
class OutcomeLine:
    week_start: date
    passed: int
    failed: int
    stopped: int
    pass_rate: str


@dataclass(frozen=True, slots=True)
class DayLine:
    day: date
    games: str


@dataclass(frozen=True, slots=True)
class ProgressPage:
    title: str
    engine: str | None
    window: Window
    start: date
    end: date
    tiles: list[Tile]
    windows: list[WindowOption]
    engines: list[str]
    greens: list[GreenLine]
    greens_hidden: int
    weekly: list[OutcomeLine]
    daily: list[DayLine]
    contributors: list[ShareLine]
    authors: list[ShareLine]


def count(value: int) -> str:
    return f"{value:,}"


def signed(value: float, digits: int = 2) -> str:
    text = f"{abs(value):.{digits}f}"
    if float(text) == 0:
        return text
    return ("+" if value > 0 else MINUS) + text


def fixed(value: float, digits: int = 2) -> str:
    return f"{value:.{digits}f}".replace("-", MINUS)


def percent(fraction: float | None, digits: int = 0) -> str:
    return DASH if fraction is None else f"{100 * fraction:.{digits}f}%"


def elo_text(interval: EloInterval | None) -> str:
    if finite_value(interval) is None or interval is None:
        return DASH
    half = max(interval.upper - interval.value, interval.value - interval.lower)
    return f"{signed(interval.value)} ± {fixed(half)}"


def progress_url(engine: str | None, window: Window) -> str:
    path = "/progress/" if engine is None else f"/progress/{quote(engine, safe='')}/"
    return f"{path}?{urlencode({'window': window.value})}"


def plural(value: int, noun: str) -> str:
    return f"{count(value)} {noun}{'' if value == 1 else 's'}"


def summary_tiles(summary: Summary) -> list[Tile]:
    sprt = summary.sprt
    decided = sprt.passed + sprt.failed
    unrated = (
        f" · {count(summary.greens_without_elo)} without an estimate"
        if summary.greens_without_elo
        else ""
    )
    per_day = (
        f"≈ {count(round(summary.games_per_day))} per day"
        if summary.games_per_day is not None
        else DASH
    )
    return [
        Tile(
            "Elo gained (estimate)",
            signed(summary.elo_gained, 1) if summary.greens else DASH,
            f"sum over {plural(summary.greens, 'green')}{unrated}",
            "pass",
        ),
        Tile(
            "SPRTs finished",
            count(sprt.total),
            f"{count(sprt.passed)} passed · {count(sprt.failed)} failed · "
            f"{count(sprt.stopped)} stopped",
        ),
        Tile(
            "SPRT pass rate",
            percent(summary.sprt_pass_rate),
            f"of {plural(decided, 'decided test')}",
            "info",
        ),
        Tile("Games played", count(summary.games), per_day),
        Tile(
            "Tests created",
            count(summary.tests_created),
            f"by {plural(summary.authors, 'author')}",
        ),
        Tile(
            "Contributors", count(summary.contributors), "users whose machines played"
        ),
    ]


def green_lines(greens: list[GreenTest]) -> list[GreenLine]:
    return [
        GreenLine(
            url=f"/test/{green.id}/",
            name=green.name,
            finished_on=utc_day(green.finished_at),
            elo=elo_text(green.elo),
            bounds=f"[{fixed(green.elo_bounds[0])}, {fixed(green.elo_bounds[1])}]",
            games=count(green.games),
            cumulative=signed(green.cumulative_elo, 1),
        )
        for green in reversed(greens[-GREENS_LISTED:])
    ]


def outcome_lines(weeks: list[WeeklyOutcomes]) -> list[OutcomeLine]:
    return [
        OutcomeLine(
            week_start=week.week_start,
            passed=week.passed,
            failed=week.failed,
            stopped=week.stopped,
            pass_rate=percent(
                week.passed / (week.passed + week.failed)
                if week.passed + week.failed
                else None
            ),
        )
        for week in reversed(weeks)
    ]


def day_lines(days: list[DailyGames]) -> list[DayLine]:
    return [DayLine(day.day, count(day.games)) for day in reversed(days) if day.games]


def share_line(
    name: str, value: int, fraction: float | None, url: str | None
) -> ShareLine:
    return ShareLine(
        name=name,
        url=url,
        value=count(value),
        share=f"{fraction or 0:.4f}",
        percent=percent(fraction, 1),
    )


def contributor_lines(rows: Iterable[Contributor]) -> list[ShareLine]:
    return [share_line(row.username, row.games, row.share, None) for row in rows]


def author_lines(rows: Iterable[Author]) -> list[ShareLine]:
    return [
        share_line(
            row.username, row.tests, row.share, f"/user/{quote(row.username, safe='')}/"
        )
        for row in rows
    ]


def engine_choices(configured: Iterable[str], current: str | None) -> list[str]:
    names = set(configured)
    if current is not None:
        names.add(current)
    return sorted(names, key=str.casefold)


def progress_page(report: ProgressReport, configured: Iterable[str]) -> ProgressPage:
    return ProgressPage(
        title=f"{report.engine} progress" if report.engine else "Engine progress",
        engine=report.engine,
        window=report.window,
        start=report.start,
        end=report.end,
        tiles=summary_tiles(report.summary),
        windows=[
            WindowOption(
                window.label,
                progress_url(report.engine, window),
                window == report.window,
            )
            for window in Window
        ],
        engines=engine_choices(configured, report.engine),
        greens=green_lines(report.greens),
        greens_hidden=max(0, len(report.greens) - GREENS_LISTED),
        weekly=outcome_lines(report.weekly_outcomes),
        daily=day_lines(report.daily_games),
        contributors=contributor_lines(report.top_contributors),
        authors=author_lines(report.top_authors),
    )
