from datetime import datetime

from django.utils import timezone

from OpenBench.progress import analysis, sources
from OpenBench.progress.domain import ProgressReport, Window


def progress_report(
    window: Window, engine: str | None, now: datetime | None = None
) -> ProgressReport:
    now = now or timezone.now()
    scope = analysis.make_scope(window, engine, now)

    greens = analysis.green_tests(sources.load_greens(scope))
    outcomes = sources.load_weekly_outcomes(scope)
    maxima = sources.load_day_maxima(scope)
    totals = analysis.games_by_day(maxima, sources.load_baselines(scope))
    games_by_user = sources.load_games_by_user(scope)
    tests_by_author = sources.load_tests_by_author(scope)

    start = analysis.series_start(
        scope,
        [
            min(totals, default=None),
            min(outcomes, default=None),
            min(
                (analysis.utc_day(green.finished_at) for green in greens), default=None
            ),
        ],
    )
    daily = analysis.daily_series(totals, start, scope.today)

    return ProgressReport(
        generated_at=now,
        engine=engine,
        window=window,
        start=start,
        end=scope.today,
        summary=analysis.summarize(
            greens, outcomes.values(), daily, tests_by_author, games_by_user
        ),
        greens=greens,
        weekly_outcomes=analysis.weekly_series(outcomes, start, scope.today),
        daily_games=daily,
        top_contributors=analysis.top_contributors(games_by_user),
        top_authors=analysis.top_authors(tests_by_author),
    )
