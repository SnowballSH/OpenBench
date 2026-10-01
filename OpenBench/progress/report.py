from datetime import datetime

from django.utils import timezone

from OpenBench.progress import analysis, sources
from OpenBench.progress.conditions import time_class
from OpenBench.progress.domain import NO_LINEAGE, Lineage, ProgressReport, RunRow, Window
from OpenBench.progress.economics import economics
from OpenBench.progress.lineage import build_lineage, build_steps, lineage_report, summarize_lineage


def engines_with_steps(runs: list[RunRow]) -> list[str]:
    return sorted({run.engine for run in runs if run.base != run.dev}, key=str.casefold)


def lineage_engine(selected: str | None, engines: list[str]) -> str | None:
    if selected is not None:
        return selected
    return engines[0] if len(engines) == 1 else None


def engine_lineage(runs: list[RunRow], engine: str | None) -> Lineage | None:
    if engine is None:
        return None
    return build_lineage(build_steps(run for run in runs if run.engine == engine))


def progress_report(window: Window, engine: str | None, now: datetime | None = None) -> ProgressReport:
    now = now or timezone.now()
    scope = analysis.make_scope(window, engine, now)

    runs = sources.load_runs(scope.engine, time_class, sources.load_usage(scope.engine))
    engines = engines_with_steps(runs)
    charted = lineage_engine(engine, engines)
    lineage = engine_lineage(runs, charted)

    outcomes = sources.load_weekly_outcomes(scope)
    maxima = sources.load_day_maxima(scope)
    totals = analysis.games_by_day(maxima, sources.load_baselines(scope))
    games_by_user = sources.load_games_by_user(scope)
    tests_by_author = sources.load_tests_by_author(scope)

    start = analysis.series_start(scope, [min(totals, default=None), min(outcomes, default=None)])
    daily = analysis.daily_series(totals, start, scope.today)

    return ProgressReport(
        generated_at=now,
        engine=engine,
        window=window,
        start=start,
        end=scope.today,
        summary=analysis.summarize(
            summarize_lineage(lineage, scope.since) if lineage else NO_LINEAGE,
            outcomes.values(),
            daily,
            tests_by_author,
            games_by_user,
        ),
        lineage=lineage_report(charted, lineage, scope.since) if charted and lineage else None,
        economics=economics(lineage, scope.since, start, scope.today) if charted and lineage else None,
        lineage_engines=engines,
        weekly_outcomes=analysis.weekly_series(outcomes, start, scope.today),
        daily_games=daily,
        top_contributors=analysis.top_contributors(games_by_user),
        top_authors=analysis.top_authors(tests_by_author),
    )
