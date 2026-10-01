from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from urllib.parse import quote, urlencode

from OpenBench.insights.strength import EloInterval
from OpenBench.progress.analysis import share, utc_day
from OpenBench.progress.domain import (
    Author,
    Cadence,
    Candidate,
    ChainSeries,
    ClassEconomics,
    Commit,
    Contributor,
    CostBucket,
    DailyGames,
    DirectCheck,
    Economics,
    LineageReport,
    Measurement,
    ProgressReport,
    RatioInterval,
    Run,
    RunStatus,
    SpeedPoint,
    SpeedRatio,
    SpeedSeries,
    Step,
    StepCost,
    StepSpeed,
    Summary,
    TimeClass,
    WeeklyOutcomes,
    Window,
)
from OpenBench.progress.lineage import half_width
from OpenBench.workload_names import short_name

STEPS_LISTED = 100
HEADLINE_CLASSES = (TimeClass.STC, TimeClass.LTC)
VERDICT_TONES: dict[RunStatus, str | None] = {
    RunStatus.PASSED: 'pass',
    RunStatus.FAILED: 'fail',
    RunStatus.RUNNING: 'info',
    RunStatus.PENDING: 'warn',
    RunStatus.COMPLETED: None,
    RunStatus.STOPPED: None,
}
DASH = '—'
MINUS = '−'
DEEPEST_INDENT = 3
SECONDS_PER_MINUTE = 60
SECONDS_PER_HOUR = 3600
SECONDS_PER_DAY = 86400
LONGEST_IN_MINUTES = 90 * SECONDS_PER_MINUTE
LONGEST_IN_HOURS = 48 * SECONDS_PER_HOUR


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
class RunLink:
    url: str
    label: str
    primary: bool = False


@dataclass(frozen=True, slots=True)
class CommitLink:
    label: str
    url: str | None


@dataclass(frozen=True, slots=True)
class Cell:
    label: str
    elo: str
    detail: str
    provisional: bool
    verdict: str
    tone: str | None
    runs: list[RunLink]


@dataclass(frozen=True, slots=True)
class StepLine:
    on_trunk: bool
    position: str
    depth: int
    base: CommitLink
    dev: CommitLink
    compare_url: str | None
    subject: str
    tested_on: date
    author: str
    cells: list[Cell | None]
    row_url: str | None
    bench: str = ''
    economics: str = ''


@dataclass(frozen=True, slots=True)
class ClassLine:
    label: str
    passed: str
    failed: str
    pass_rate: str
    games_to_pass: str
    games_to_fail: str
    games: str
    core_hours: str


@dataclass(frozen=True, slots=True)
class SpeedLine:
    position: str
    step: str
    hosts: str
    cumulative: str


@dataclass(frozen=True, slots=True)
class WeekLine:
    week_start: date
    steps: int


@dataclass(frozen=True, slots=True)
class EconomicsPage:
    tiles: list[Tile]
    spending: str
    classes: list[ClassLine]
    speed: list[SpeedLine]
    weeks: list[WeekLine]


@dataclass(frozen=True, slots=True)
class CheckLine:
    label: str
    base: CommitLink
    dev: CommitLink
    span: str
    direct: Cell
    chained: str
    coverage: str
    difference: str
    row_url: str | None


@dataclass(frozen=True, slots=True)
class LineagePage:
    engine: str
    classes: list[str]
    columns: int
    lines: list[StepLine]
    origin: CommitLink
    steps_hidden: int
    candidates_hidden: int
    checks: list[CheckLine]
    detached: list[StepLine]
    detached_hidden: int
    others: str


@dataclass(frozen=True, slots=True)
class EngineLink:
    name: str
    url: str


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
    lineage: LineagePage | None
    economics: EconomicsPage | None
    lineage_engines: list[EngineLink]
    weekly: list[OutcomeLine]
    daily: list[DayLine]
    contributors: list[ShareLine]
    authors: list[ShareLine]


def count(value: int) -> str:
    return f'{value:,}'


def signed(value: float, digits: int = 2) -> str:
    text = f'{abs(value):.{digits}f}'
    if float(text) == 0:
        return text
    return ('+' if value > 0 else MINUS) + text


def fixed(value: float, digits: int = 2) -> str:
    return f'{value:.{digits}f}'.replace('-', MINUS)


def percent(fraction: float | None, digits: int = 0) -> str:
    return DASH if fraction is None else f'{100 * fraction:.{digits}f}%'


def elo_text(interval: EloInterval | None, digits: int = 2) -> str:
    if interval is None:
        return DASH
    return f'{signed(interval.value, digits)} ± {fixed(half_width(interval), digits)}'


def path_safe(engine: str) -> bool:
    return '/' not in engine


def progress_url(engine: str | None, window: Window) -> str:
    if engine is None:
        return f'/progress/?{urlencode({"window": window.value})}'
    if path_safe(engine):
        return f'/progress/{quote(engine, safe="")}/?{urlencode({"window": window.value})}'
    return f'/progress/?{urlencode({"engine": engine, "window": window.value})}'


def plural(value: int, noun: str) -> str:
    return f'{count(value)} {noun}{"" if value == 1 else "s"}'


def chained_tone(total: EloInterval) -> str | None:
    if total.value > 0:
        return 'pass'
    return 'fail' if total.value < 0 else None


def chained_tile(time_class: TimeClass, series: ChainSeries | None) -> Tile:
    label = f'Chained Elo · {time_class.label}'
    if series is None:
        return Tile(label, DASH, f'no trunk step measured at {time_class.label}')
    running = f' · {count(series.provisional)} running' if series.provisional else ''
    meta = f'{count(series.measured)} of {plural(series.steps, "trunk step")} measured{running}'
    if series.total is None:
        return Tile(label, DASH, meta)
    return Tile(label, elo_text(series.total, 1), meta, chained_tone(series.total))


def summary_tiles(summary: Summary, series: Iterable[ChainSeries] = ()) -> list[Tile]:
    sprt = summary.sprt
    lineage = summary.lineage
    decided = sprt.passed + sprt.failed
    per_day = f'≈ {count(round(summary.games_per_day))} per day' if summary.games_per_day is not None else DASH
    by_class = {found.time_class: found for found in series}
    return [
        *(chained_tile(time_class, by_class.get(time_class)) for time_class in HEADLINE_CLASSES),
        Tile(
            'Trunk steps',
            count(lineage.trunk_steps),
            f'{plural(lineage.candidates, "candidate")} off the trunk',
        ),
        Tile('Measurements', count(lineage.measurements), f'pooled from {plural(lineage.runs, "run")}'),
        Tile(
            'SPRT pass rate',
            percent(summary.sprt_pass_rate),
            f'{count(sprt.passed)} of {plural(decided, "decided test")} · {count(sprt.stopped)} stopped',
            'info',
        ),
        Tile('Games played', count(summary.games), per_day),
    ]


def repo_url(repo: str) -> str | None:
    return repo.rstrip('/') if repo.startswith('https://') else None


def commit_link(commit: Commit, repo: str, with_network: bool = False) -> CommitLink:
    label = short_name(commit.sha)
    if with_network and commit.network:
        label = f'{label} · {commit.network}'
    root = repo_url(repo)
    return CommitLink(label, f'{root}/commit/{quote(commit.sha, safe="")}' if root else None)


def compare_url(step: Step) -> str | None:
    root = repo_url(step.repo)
    if root is None or step.base.sha == step.dev.sha:
        return None
    return f'{root}/compare/{quote(step.base.sha, safe="")}...{quote(step.dev.sha, safe="")}'


def run_url(run: Run) -> str:
    return f'/test/{run.id}/'


def sole_run(measurements: Iterable[Measurement]) -> Run | None:
    runs = [run for measurement in measurements for run in measurement.runs]
    return runs[0] if len(runs) == 1 else None


def measurement_cell(measurement: Measurement, primary: Run | None = None) -> Cell:
    runs = measurement.runs
    controls = sorted({run.time_control for run in runs})
    return Cell(
        label=measurement.time_class.label,
        elo=elo_text(measurement.elo),
        detail=' · '.join([f'{count(measurement.games)} games', *controls]),
        provisional=measurement.provisional,
        verdict=measurement.verdict.value,
        tone=VERDICT_TONES[measurement.verdict],
        runs=[RunLink(run_url(run), f'#{run.id}', run is primary) for run in runs],
    )


def change_text(ratio: float, digits: int = 1) -> str:
    return f'{signed(100 * (ratio - 1), digits)}%'


def range_text(speed: SpeedRatio | RatioInterval) -> str:
    if speed.lower is None or speed.upper is None:
        return ''
    return f'{change_text(speed.lower)} to {change_text(speed.upper)}'


def speed_text(speed: StepSpeed | None) -> str:
    if speed is None:
        return ''
    pooled = speed.pooled
    spread = range_text(pooled) or ('one host, no interval' if pooled.hosts == 1 else 'one host dominates, no interval')
    by_class = ', '.join(f'{found.time_class.label} {change_text(found.speed.ratio)}' for found in speed.classes)
    return f'speed {change_text(pooled.ratio)} ({spread})' + (f'; {by_class}' if speed.classes_differ else '')


def duration_text(seconds: float | None) -> str:
    if seconds is None:
        return DASH
    if seconds < LONGEST_IN_MINUTES:
        return f'{seconds / SECONDS_PER_MINUTE:.0f} min'
    if seconds < LONGEST_IN_HOURS:
        return f'{seconds / SECONDS_PER_HOUR:.1f} h'
    return f'{seconds / SECONDS_PER_DAY:.1f} d'


def core_hours_text(hours: float) -> str:
    return f'{hours:,.1f} core-h'


def cost_text(cost: StepCost) -> str:
    parts = [f'{count(cost.games)} games']
    if cost.decision_seconds is not None:
        parts.append(f'{duration_text(cost.decision_seconds)} under test')
    if cost.core_hours is not None:
        parts.append(core_hours_text(cost.core_hours))
    return ' · '.join(parts)


def bench_text(step: Step) -> str:
    if step.dev_bench is None:
        return ''
    unchanged = ', same as base' if step.dev_bench == step.base_bench and step.base.sha != step.dev.sha else ''
    return f'bench {count(step.dev_bench)}{unchanged}'


def step_economics(step: Step) -> str:
    return ' · '.join(part for part in (speed_text(step.speed), cost_text(step.cost)) if part)


def step_cells(step: Step, classes: Iterable[TimeClass], primary: Run | None) -> list[Cell | None]:
    return [
        measurement_cell(found, primary) if (found := step.measurement(time_class)) else None for time_class in classes
    ]


def step_line(step: Step, classes: Iterable[TimeClass], position: str = '', depth: int = 0) -> StepLine:
    with_network = step.base.network != step.dev.network
    shown = [found for time_class in classes if (found := step.measurement(time_class))]
    primary = sole_run(shown) if len(shown) == len(step.measurements) else None
    return StepLine(
        on_trunk=bool(position),
        position=position,
        depth=min(depth, DEEPEST_INDENT),
        base=commit_link(step.base, step.repo, with_network),
        dev=commit_link(step.dev, step.repo, with_network),
        compare_url=compare_url(step),
        subject=step.subject,
        tested_on=utc_day(step.measured_at),
        author=step.author,
        cells=step_cells(step, classes, primary),
        row_url=run_url(primary) if primary else None,
        bench=bench_text(step),
        economics=step_economics(step),
    )


def candidate_lines(candidates: Iterable[Candidate], classes: Iterable[TimeClass]) -> list[StepLine]:
    return [step_line(found.step, classes, depth=found.depth) for found in candidates]


def lineage_lines(lineage: LineageReport) -> list[StepLine]:
    lines: list[StepLine] = []
    for row in reversed(lineage.steps[-STEPS_LISTED:]):
        lines.extend(candidate_lines(row.candidates, lineage.classes))
        lines.append(step_line(row.step, lineage.classes, position=str(row.index)))
    if len(lineage.steps) <= STEPS_LISTED and not lineage.steps_omitted:
        lines.extend(candidate_lines(lineage.origin_candidates, lineage.classes))
    return lines


def difference_text(check: DirectCheck) -> str:
    if check.direct.elo is None or check.direct.provisional or check.chained is None:
        return DASH
    margin = (half_width(check.direct.elo) ** 2 + half_width(check.chained) ** 2) ** 0.5
    return f'{signed(check.direct.elo.value - check.chained.value)} ± {fixed(margin)}'


def check_line(check: DirectCheck) -> CheckLine:
    primary = sole_run([check.direct])
    return CheckLine(
        label=check.time_class.label,
        base=commit_link(check.base, check.repo),
        dev=commit_link(check.dev, check.repo),
        span=f'steps {check.first_index}–{check.last_index}',
        direct=measurement_cell(check.direct, primary),
        chained=elo_text(check.chained),
        coverage=f'{count(check.measured)} of {plural(check.steps, "step")} measured',
        difference=difference_text(check),
        row_url=run_url(primary) if primary else None,
    )


def network_changes(lineage: LineageReport) -> bool:
    return any(row.step.base.network != row.step.dev.network for row in lineage.steps)


def others_note(lineage: LineageReport) -> str:
    if not lineage.others:
        return ''
    steps = sum(other.steps for other in lineage.others)
    return f'{plural(steps, "step")} in {plural(len(lineage.others), "other lineage")}'


def repo_of(lineage: LineageReport) -> str:
    steps = [row.step for row in lineage.steps] + [found.step for found in lineage.origin_candidates]
    return steps[0].repo if steps else ''


def lineage_page(lineage: LineageReport) -> LineagePage:
    listed = lineage.steps[-STEPS_LISTED:]
    return LineagePage(
        engine=lineage.engine,
        classes=[time_class.label for time_class in lineage.classes],
        columns=2 + len(lineage.classes),
        lines=lineage_lines(lineage),
        origin=commit_link(lineage.origin, repo_of(lineage), with_network=network_changes(lineage)),
        steps_hidden=lineage.steps_omitted + len(lineage.steps) - len(listed),
        candidates_hidden=lineage.origin_candidates_omitted + sum(row.candidates_omitted for row in listed),
        checks=[check_line(check) for check in lineage.direct],
        detached=[step_line(step, [found.time_class for found in step.measurements]) for step in lineage.detached],
        detached_hidden=lineage.detached_omitted,
        others=others_note(lineage),
    )


def speed_tile(series: SpeedSeries) -> Tile:
    label = 'Speed along the trunk'
    coverage = f'{count(series.measured)} of {plural(series.steps, "step")} measured'
    if series.total is None:
        return Tile(label, DASH, coverage)
    spread = range_text(series.total)
    unbounded = f'no interval: {plural(series.unbounded, "step")} without host spread'
    return Tile(label, change_text(series.total.ratio), f'{f"95% {spread}" if spread else unbounded} · {coverage}')


def failed_tile(economics: Economics) -> Tile:
    label = 'Spent on failed changes'
    bucket = economics.failed
    tested = economics.trunk.steps + bucket.steps + economics.other.steps
    steps = f'{count(bucket.steps)} of {plural(tested, "tested change")}'
    if bucket.core_share is None:
        return Tile(label, percent(bucket.games_share), f'of games (no node counters) · {steps}')
    basis = (
        f'of search time, estimated: counters cover {percent(economics.counter_coverage)} of games'
        if economics.core_hours_estimated
        else 'of search time'
    )
    return Tile(label, percent(bucket.core_share), f'{basis}; {percent(bucket.games_share)} of games · {steps}')


def games_per_elo_tile(row: ClassEconomics | None, time_class: TimeClass) -> Tile:
    label = f'Games per Elo · {time_class.label}'
    if row is None or row.games_per_elo is None or row.chained_elo is None:
        return Tile(label, DASH, f'no positive chained Elo at {time_class.label}')
    unsure = '; within its margin of zero' if row.chained_elo.lower <= 0 else ''
    return Tile(
        label,
        count(round(row.games_per_elo)),
        f'{count(row.finished_games)} games of finished tests ÷ {signed(row.chained_elo.value, 1)} chained{unsure}'
        ' · optimistic',
    )


def velocity_tile(cadence: Cadence) -> Tile:
    label = 'Trunk velocity'
    span = plural(cadence.span_days, 'day')
    if cadence.steps_per_week is None:
        value = f'{plural(cadence.joined, "step")} in {span}' if cadence.joined else DASH
        return Tile(label, value, 'settled trunk steps; a weekly rate needs a week of history')
    return Tile(label, f'{cadence.steps_per_week:.1f} / week', f'{plural(cadence.joined, "settled step")} in {span}')


def latency_tile(cadence: Cadence) -> Tile:
    confirmation = (
        f'STC pass to LTC pass {duration_text(cadence.median_confirmation_seconds)}'
        f' ({count(cadence.confirmation_samples)})'
    )
    return Tile(
        'First test to accepted',
        duration_text(cadence.median_acceptance_seconds),
        f'median of {plural(cadence.acceptance_samples, "settled step")}'
        f' · mean {duration_text(cadence.mean_acceptance_seconds)} · {confirmation}',
    )


def cadence_tiles(cadence: Cadence) -> list[Tile]:
    return [velocity_tile(cadence), latency_tile(cadence)]


def economics_tiles(economics: Economics) -> list[Tile]:
    by_class = {row.time_class: row for row in economics.classes}
    return [
        speed_tile(economics.speed),
        failed_tile(economics),
        *(games_per_elo_tile(by_class.get(time_class), time_class) for time_class in HEADLINE_CLASSES),
        *cadence_tiles(economics.cadence),
    ]


def bucket_hours(bucket: CostBucket) -> str:
    if bucket.counted_games >= bucket.games:
        return core_hours_text(bucket.core_hours)
    covered = percent(share(bucket.counted_games, bucket.games))
    if bucket.estimated_core_hours is None:
        return f'{core_hours_text(bucket.core_hours)} counted over {covered} of its games'
    return f'about {core_hours_text(bucket.estimated_core_hours)} (estimated; counters cover {covered} of its games)'


def bucket_text(name: str, bucket: CostBucket) -> str:
    return f'{plural(bucket.steps, name)}: {count(bucket.games)} games, {bucket_hours(bucket)}'


def spending_text(economics: Economics) -> str:
    parts = [
        bucket_text('trunk step', economics.trunk),
        bucket_text('failed candidate', economics.failed),
        bucket_text('other candidate', economics.other),
    ]
    coverage = f'Node counters cover {percent(economics.counter_coverage)} of games.'
    return f'{"; ".join(parts)}. {coverage}'


def rounded(value: float | None) -> str:
    return DASH if value is None else count(round(value))


def class_line(row: ClassEconomics) -> ClassLine:
    return ClassLine(
        label=row.time_class.label,
        passed=count(row.passed),
        failed=count(row.failed),
        pass_rate=percent(row.pass_rate),
        games_to_pass=rounded(row.median_games_to_pass),
        games_to_fail=rounded(row.median_games_to_fail),
        games=count(row.games),
        core_hours=f'{row.core_hours:,.1f}',
    )


def speed_line(point: SpeedPoint) -> SpeedLine:
    step, total = point.step, point.cumulative
    return SpeedLine(
        position=str(point.index),
        step=DASH if step is None else f'{change_text(step.ratio)} {f"({range_text(step)})" if step.lower else ""}',
        hosts=DASH if step is None else count(step.hosts),
        cumulative=DASH
        if step is None or total is None
        else f'{change_text(total.ratio)} {f"({range_text(total)})" if total.lower else ""}',
    )


def economics_page(economics: Economics) -> EconomicsPage:
    return EconomicsPage(
        tiles=economics_tiles(economics),
        spending=spending_text(economics),
        classes=[class_line(row) for row in economics.classes],
        speed=[speed_line(point) for point in reversed(economics.speed.points[-STEPS_LISTED:])],
        weeks=[WeekLine(week.week_start, week.steps) for week in reversed(economics.cadence.weekly)],
    )


def outcome_lines(weeks: list[WeeklyOutcomes]) -> list[OutcomeLine]:
    return [
        OutcomeLine(
            week_start=week.week_start,
            passed=week.passed,
            failed=week.failed,
            stopped=week.stopped,
            pass_rate=percent(week.passed / (week.passed + week.failed) if week.passed + week.failed else None),
        )
        for week in reversed(weeks)
    ]


def day_lines(days: list[DailyGames]) -> list[DayLine]:
    return [DayLine(day.day, count(day.games)) for day in reversed(days) if day.games]


def share_line(name: str, value: int, fraction: float | None, url: str | None) -> ShareLine:
    return ShareLine(
        name=name,
        url=url,
        value=count(value),
        share=f'{fraction or 0:.4f}',
        percent=percent(fraction, 1),
    )


def contributor_lines(rows: Iterable[Contributor]) -> list[ShareLine]:
    return [share_line(row.username, row.games, row.share, None) for row in rows]


def author_lines(rows: Iterable[Author]) -> list[ShareLine]:
    return [share_line(row.username, row.tests, row.share, f'/user/{quote(row.username, safe="")}/') for row in rows]


def engine_choices(configured: Iterable[str], current: str | None) -> list[str]:
    names = set(configured)
    if current is not None:
        names.add(current)
    return sorted(names, key=str.casefold)


def progress_page(report: ProgressReport, configured: Iterable[str]) -> ProgressPage:
    return ProgressPage(
        title=f'{report.engine} progress' if report.engine else 'Engine progress',
        engine=report.engine,
        window=report.window,
        start=report.start,
        end=report.end,
        tiles=summary_tiles(report.summary, report.lineage.series if report.lineage else ()),
        windows=[
            WindowOption(
                window.label,
                progress_url(report.engine, window),
                window == report.window,
            )
            for window in Window
        ],
        engines=engine_choices(configured, report.engine),
        lineage=lineage_page(report.lineage) if report.lineage else None,
        economics=economics_page(report.economics) if report.economics else None,
        lineage_engines=[EngineLink(name, progress_url(name, report.window)) for name in report.lineage_engines],
        weekly=outcome_lines(report.weekly_outcomes),
        daily=day_lines(report.daily_games),
        contributors=contributor_lines(report.top_contributors),
        authors=author_lines(report.top_authors),
    )
