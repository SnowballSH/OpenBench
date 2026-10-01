from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
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

type Rates = Mapping[TimeClass, float]


def window_candidates(lineage: Lineage, shown: Sequence[Step]) -> list[Step]:
    origin = shown[0].base if shown else lineage.head
    nodes = [origin, *(step.dev for step in shown)]
    return [found.step for node in nodes for found in lineage.branches.get(node, [])]


def failed(step: Step) -> bool:
    return any(measurement.verdict == RunStatus.FAILED for measurement in step.measurements)


def class_runs(steps: Iterable[Step]) -> list[tuple[TimeClass, Run]]:
    return [
        (measurement.time_class, run) for step in steps for measurement in step.measurements for run in measurement.runs
    ]


def class_rates(runs: Iterable[tuple[TimeClass, Run]]) -> Rates:
    hours: defaultdict[TimeClass, float] = defaultdict(float)
    games: Counter[TimeClass] = Counter()
    for time_class, run in runs:
        if run.core_hours is not None and run.counted_games:
            hours[time_class] += run.core_hours
            games[time_class] += run.counted_games
    return {time_class: hours[time_class] / games[time_class] for time_class in games}


def hours_per_game(steps: Iterable[Step]) -> Rates:
    return class_rates(class_runs(steps))


def estimated_hours(time_class: TimeClass, run: Run, rates: Rates) -> float | None:
    if run.core_hours is not None and run.counted_games >= run.games:
        return run.core_hours
    if run.core_hours is not None and run.counted_games:
        return run.core_hours * run.games / run.counted_games
    if not run.games:
        return 0.0
    rate = rates.get(time_class)
    return None if rate is None else rate * run.games


def estimated_core_hours(steps: Iterable[Step], rates: Rates) -> float | None:
    estimates = [estimated_hours(time_class, run, rates) for time_class, run in class_runs(steps)]
    return None if None in estimates else sum(found for found in estimates if found is not None)


def bucket(steps: Sequence[Step], rates: Rates) -> CostBucket:
    return CostBucket(
        steps=len(steps),
        runs=sum(step.cost.runs for step in steps),
        games=sum(step.cost.games for step in steps),
        core_hours=sum(step.cost.core_hours or 0.0 for step in steps),
        counted_games=sum(step.cost.counted_games for step in steps),
        estimated_core_hours=estimated_core_hours(steps, rates),
        games_share=None,
        core_share=None,
    )


def with_shares(buckets: Sequence[CostBucket]) -> list[CostBucket]:
    games = sum(found.games for found in buckets)
    estimates = [found.estimated_core_hours for found in buckets]
    hours = None if None in estimates else sum(found for found in estimates if found is not None)
    return [
        replace(
            found,
            games_share=share(found.games, games),
            core_share=found.estimated_core_hours / hours if hours and found.estimated_core_hours is not None else None,
        )
        for found in buckets
    ]


def estimated(found: CostBucket) -> bool:
    return found.estimated_core_hours is not None and found.counted_games < found.games


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
    finished_games = sum(run.games for run in runs if run.finished_at is not None)
    chained = chain_series(trunk, time_class, 1).total if time_class.chained else None
    return ClassEconomics(
        time_class=time_class,
        passed=passed,
        failed=rejected,
        pass_rate=share(passed, passed + rejected),
        median_games_to_pass=median_games(sprt, RunStatus.PASSED),
        median_games_to_fail=median_games(sprt, RunStatus.FAILED),
        games=sum(run.games for run in runs),
        finished_games=finished_games,
        core_hours=sum(run.core_hours or 0.0 for run in runs),
        chained_elo=chained,
        games_per_elo=finished_games / chained.value if chained and chained.value > 0 else None,
    )


def runs_of(step: Step) -> list[Run]:
    return [run for measurement in step.measurements for run in measurement.runs]


def pass_times(runs: Iterable[Run]) -> list[datetime]:
    return [run.finished_at for run in runs if run.status == RunStatus.PASSED and run.finished_at is not None]


def settled(step: Step) -> bool:
    return all(run.finished_at is not None for run in runs_of(step))


def accepted_at(step: Step) -> datetime | None:
    return max(pass_times(runs_of(step)), default=None) if settled(step) else None


def joined_at(step: Step) -> datetime | None:
    if not settled(step):
        return None
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
    span_days = max(1, (end - first).days + 1)
    return Cadence(
        weekly=weekly_steps(days, start, end),
        joined=len(days),
        span_days=span_days,
        steps_per_week=DAYS_PER_WEEK * len(days) / span_days if days and span_days >= DAYS_PER_WEEK else None,
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
    rates = hours_per_game(everything)
    on_trunk, lost, rest = with_shares([bucket(trunk, rates), bucket(rejected, rates), bucket(remaining, rates)])
    games = sum(step.cost.games for step in everything)
    counted = sum(step.cost.counted_games for step in everything)
    tested = classes_tested(everything)
    speed = speed_series(trunk, offset + 1)

    return Economics(
        trunk=on_trunk,
        failed=lost,
        other=rest,
        counter_coverage=share(counted, games),
        core_hours_estimated=any(estimated(found) for found in (on_trunk, lost, rest)),
        classes=[class_economics(time_class, trunk, candidates) for time_class in TimeClass if time_class in tested],
        speed=replace(speed, points=speed.points[-TRUNK_STEPS_SENT:]),
        cadence=cadence(trunk, start, end),
    )
