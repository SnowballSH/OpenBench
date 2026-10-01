from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import replace
from datetime import date, datetime
from statistics import mean, median

from OpenBench.progress.analysis import days_between, share, utc_day, week_start
from OpenBench.progress.domain import (
    TRUNK_STEPS_SENT,
    Cadence,
    ClassEconomics,
    CostBucket,
    Economics,
    Lineage,
    Run,
    RunMode,
    RunStatus,
    Step,
    TimeClass,
    WeeklySteps,
)
from OpenBench.progress.lineage import chain_series, window_offset
from OpenBench.progress.speed import speed_series

DAYS_PER_WEEK = 7


def window_candidates(lineage: Lineage, shown: Sequence[Step]) -> list[Step]:
    origin = shown[0].base if shown else lineage.head
    nodes = [origin, *(step.dev for step in shown)]
    return [found.step for node in nodes for found in lineage.branches.get(node, [])]


def failed(step: Step) -> bool:
    return any(measurement.verdict == RunStatus.FAILED for measurement in step.measurements)


def bucket(steps: Sequence[Step], games: int, core_hours: float) -> CostBucket:
    spent = sum(step.cost.core_hours or 0.0 for step in steps)
    played = sum(step.cost.games for step in steps)
    return CostBucket(
        steps=len(steps),
        runs=sum(step.cost.runs for step in steps),
        games=played,
        core_hours=spent,
        counted_games=sum(step.cost.counted_games for step in steps),
        games_share=share(played, games),
        core_share=spent / core_hours if core_hours else None,
    )


def runs_at(steps: Iterable[Step], time_class: TimeClass) -> list[Run]:
    return [run for step in steps if (found := step.measurement(time_class)) for run in found.runs]


def median_games(runs: Iterable[Run], status: RunStatus) -> float | None:
    games = [run.games for run in runs if run.status == status]
    return float(median(games)) if games else None


def class_economics(time_class: TimeClass, trunk: Sequence[Step], candidates: Sequence[Step]) -> ClassEconomics:
    runs = runs_at([*trunk, *candidates], time_class)
    sprt = [run for run in runs if run.mode == RunMode.SPRT]
    passed = sum(run.status == RunStatus.PASSED for run in sprt)
    rejected = sum(run.status == RunStatus.FAILED for run in sprt)
    games = sum(run.games for run in runs)
    chained = chain_series(trunk, time_class, 1).total if time_class.chained else None
    return ClassEconomics(
        time_class=time_class,
        passed=passed,
        failed=rejected,
        pass_rate=share(passed, passed + rejected),
        median_games_to_pass=median_games(sprt, RunStatus.PASSED),
        median_games_to_fail=median_games(sprt, RunStatus.FAILED),
        games=games,
        core_hours=sum(run.core_hours or 0.0 for run in runs),
        chained_elo=chained,
        games_per_elo=games / chained.value if chained and chained.value > 0 else None,
    )


def runs_of(step: Step) -> list[Run]:
    return [run for measurement in step.measurements for run in measurement.runs]


def pass_times(runs: Iterable[Run]) -> list[datetime]:
    return [run.finished_at for run in runs if run.status == RunStatus.PASSED and run.finished_at is not None]


def accepted_at(step: Step) -> datetime | None:
    return max(pass_times(runs_of(step)), default=None)


def joined_at(step: Step) -> datetime | None:
    finishes = [run.finished_at for run in runs_of(step) if run.finished_at is not None]
    return accepted_at(step) or max(finishes, default=None)


def acceptance_seconds(step: Step) -> float | None:
    accepted = accepted_at(step)
    return None if accepted is None else (accepted - step.first_tested_at).total_seconds()


def confirmation_seconds(step: Step) -> float | None:
    short, long = step.measurement(TimeClass.STC), step.measurement(TimeClass.LTC)
    if short is None or long is None:
        return None
    first_pass = min(pass_times(short.runs), default=None)
    if first_pass is None:
        return None
    confirmed = min((moment for moment in pass_times(long.runs) if moment >= first_pass), default=None)
    return None if confirmed is None else (confirmed - first_pass).total_seconds()


def weekly_steps(days: Sequence[date], start: date, end: date) -> list[WeeklySteps]:
    counts = Counter(week_start(day) for day in days)
    first = min([start, *days])
    weeks = days_between(week_start(first), week_start(end))[::DAYS_PER_WEEK]
    return [WeeklySteps(week, counts.get(week, 0)) for week in weeks]


def cadence(trunk: Sequence[Step], start: date, end: date) -> Cadence:
    days = [utc_day(moment) for step in trunk if (moment := joined_at(step)) is not None]
    tested = [utc_day(step.first_tested_at) for step in trunk]
    first = max(start, min(tested, default=start))
    acceptances = [found for step in trunk if (found := acceptance_seconds(step)) is not None]
    confirmations = [found for step in trunk if (found := confirmation_seconds(step)) is not None]
    return Cadence(
        weekly=weekly_steps(days, start, end),
        joined=len(days),
        steps_per_week=DAYS_PER_WEEK * len(days) / max(1, (end - first).days + 1) if days else None,
        acceptance_samples=len(acceptances),
        median_acceptance_seconds=median(acceptances) if acceptances else None,
        mean_acceptance_seconds=mean(acceptances) if acceptances else None,
        confirmation_samples=len(confirmations),
        median_confirmation_seconds=median(confirmations) if confirmations else None,
    )


def classes_tested(steps: Iterable[Step]) -> set[TimeClass]:
    return {measurement.time_class for step in steps for measurement in step.measurements}


def economics(lineage: Lineage, since: datetime | None, start: date, end: date) -> Economics:
    offset = window_offset(lineage.trunk, since)
    trunk = lineage.trunk[offset:]
    candidates = window_candidates(lineage, trunk)
    rejected = [step for step in candidates if failed(step)]
    remaining = [step for step in candidates if not failed(step)]

    everything = [*trunk, *candidates]
    games = sum(step.cost.games for step in everything)
    core_hours = sum(step.cost.core_hours or 0.0 for step in everything)
    counted = sum(step.cost.counted_games for step in everything)
    tested = classes_tested(everything)
    speed = speed_series(trunk, offset + 1)

    return Economics(
        trunk=bucket(trunk, games, core_hours),
        failed=bucket(rejected, games, core_hours),
        other=bucket(remaining, games, core_hours),
        counter_coverage=share(counted, games),
        classes=[class_economics(time_class, trunk, candidates) for time_class in TimeClass if time_class in tested],
        speed=replace(speed, points=speed.points[-TRUNK_STEPS_SENT:]),
        cadence=cadence(trunk, start, end),
    )
