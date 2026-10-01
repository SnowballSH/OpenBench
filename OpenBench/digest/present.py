from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlencode

from OpenBench.diagnosis.domain import Severity
from OpenBench.digest.domain import (
    ClassMovement,
    DigestReport,
    DigestWindow,
    ErrorDigest,
    FinishedDigest,
    FinishedWorkload,
    FleetActivity,
    GamesBucket,
    LlrProgress,
    PoolActivity,
    Preset,
    RunningDigest,
    RunningWorkload,
    TrunkMove,
    TrunkMovement,
    WorkloadRef,
)
from OpenBench.digest.headline import span_phrase, utc_moment
from OpenBench.fleet.status import relative_age
from OpenBench.insights.domain import WorkloadStatus
from OpenBench.insights.eta import Eta, EtaKind
from OpenBench.insights.listing import RowTimingKind, eta_parts, format_duration, format_rate
from OpenBench.progress.domain import DEFAULT_WINDOW
from OpenBench.progress.present import (
    DASH,
    VERDICT_TONES,
    CommitLink,
    RunLink,
    Tile,
    WindowOption,
    chained_tone,
    commit_link,
    count,
    elo_text,
    fixed,
    plural,
    progress_url,
)

PAGE_URL = '/digest/'
ERRORS_URL = '/errors/'
UNRESOLVED_ERRORS_URL = '/errors/?unresolved=1'

STATUS_BADGES: dict[WorkloadStatus, tuple[str, str | None]] = {
    WorkloadStatus.PASSED: ('Passed', 'pass'),
    WorkloadStatus.FAILED: ('Failed', 'fail'),
    WorkloadStatus.COMPLETED: ('Completed', 'info'),
    WorkloadStatus.STOPPED: ('Stopped', 'warn'),
    WorkloadStatus.DELETED: ('Deleted', None),
    WorkloadStatus.ACTIVE: ('Running', 'info'),
    WorkloadStatus.PENDING: ('Awaiting approval', 'warn'),
}


@dataclass(frozen=True, slots=True)
class FinishedLine:
    workload: WorkloadRef
    time_class: str
    verdict: str
    tone: str | None
    elo: str
    games: str
    took: str
    finished_at: datetime
    ago: str
    stop_reason: str | None


@dataclass(frozen=True, slots=True)
class Meter:
    fraction: float
    value: str
    label: str


@dataclass(frozen=True, slots=True)
class RunningLine:
    workload: WorkloadRef
    time_class: str
    state: str
    state_detail: str
    severity: Severity
    new: bool
    llr: Meter | None
    games: str
    elo: str
    time_left: str
    estimate: bool
    rate: str
    started_at: datetime
    ago: str


@dataclass(frozen=True, slots=True)
class MoveLine:
    position: str
    base: CommitLink
    dev: CommitLink
    subject: str
    elo: str
    provisional: bool
    remeasured: bool
    verdict: str
    tone: str | None
    games: str
    runs: list[RunLink]
    row_url: str | None
    measured_at: datetime | None
    ago: str


@dataclass(frozen=True, slots=True)
class ClassBlock:
    id: str
    label: str
    summary: str
    lines: list[MoveLine]
    omitted: int


@dataclass(frozen=True, slots=True)
class TrunkBlock:
    engine: str
    url: str
    classes: list[ClassBlock]


@dataclass(frozen=True, slots=True)
class SeriesKey:
    label: str
    name: str


@dataclass(frozen=True, slots=True)
class BucketLine:
    start: datetime
    games: list[str]
    per_hour: str


@dataclass(frozen=True, slots=True)
class FleetView:
    played: bool
    bucket: str
    series: list[SeriesKey]
    lines: list[BucketLine]
    pools: list[PoolActivity]
    pools_omitted: int
    hours_note: str


@dataclass(frozen=True, slots=True)
class DigestPage:
    generated_at: datetime
    window: DigestWindow
    span: str
    options: list[WindowOption]
    headline: list[str]
    tiles: list[Tile]
    finished: list[FinishedLine]
    finished_omitted: int
    running: list[RunningLine]
    running_omitted: int
    trunk: list[TrunkBlock]
    fleet: FleetView
    errors: ErrorDigest
    errors_url: str


def preset_url(preset: Preset) -> str:
    return f'{PAGE_URL}?{urlencode({"since": preset.value})}'


def window_options(window: DigestWindow) -> list[WindowOption]:
    presets = [WindowOption(preset.label, preset_url(preset), preset is window.preset) for preset in Preset]
    if window.preset is not None:
        return presets
    start = urlencode({'from': window.since.isoformat()})
    return [*presets, WindowOption(f'Since {utc_moment(window.since)}', f'{PAGE_URL}?{start}', True)]


def ago(moment: datetime, now: datetime) -> str:
    return relative_age(now - moment)


def finished_line(row: FinishedWorkload, now: datetime) -> FinishedLine:
    verdict, tone = STATUS_BADGES[row.status]
    return FinishedLine(
        workload=row.workload,
        time_class=row.workload.time_class.label,
        verdict=verdict,
        tone=tone,
        elo=elo_text(row.elo, 1),
        games=count(row.games),
        took=format_duration(row.duration_seconds),
        finished_at=row.finished_at,
        ago=ago(row.finished_at, now),
        stop_reason=row.stop_reason,
    )


def llr_meter(llr: LlrProgress | None) -> Meter | None:
    if llr is None or llr.upper <= llr.lower:
        return None
    fraction = min(1.0, max(0.0, (llr.value - llr.lower) / (llr.upper - llr.lower)))
    return Meter(
        fraction, fixed(llr.value), f'LLR {fixed(llr.value)} between {fixed(llr.lower)} and {fixed(llr.upper)}'
    )


def time_left(eta: Eta | None) -> tuple[str, bool]:
    if eta is None:
        return DASH, False
    kind, text = eta_parts(eta)
    return text or DASH, kind == RowTimingKind.LEFT and eta.kind == EtaKind.SPRT


def running_line(row: RunningWorkload, now: datetime) -> RunningLine:
    left, estimate = time_left(row.eta)
    return RunningLine(
        workload=row.workload,
        time_class=row.workload.time_class.label,
        state=row.diagnosis.brief,
        state_detail=row.diagnosis.headline,
        severity=row.diagnosis.severity,
        new=row.started_in_window,
        llr=llr_meter(row.llr),
        games=count(row.games),
        elo=elo_text(row.elo, 1),
        time_left=left,
        estimate=estimate,
        rate=f'{format_rate(row.games_per_hour)} games/h' if row.games_per_hour is not None else '',
        started_at=row.started_at,
        ago=ago(row.started_at, now),
    )


def move_line(move: TrunkMove, now: datetime) -> MoveLine:
    runs = [RunLink(f'/test/{run}/', f'#{run}', primary=len(move.runs) == 1) for run in move.runs]
    return MoveLine(
        position=f's{move.index}',
        base=commit_link(move.base, move.repo),
        dev=commit_link(move.dev, move.repo),
        subject=move.subject,
        elo=elo_text(move.elo),
        provisional=move.provisional,
        remeasured=move.remeasured,
        verdict=move.verdict.value,
        tone=VERDICT_TONES[move.verdict],
        games=f'{count(move.games)} games',
        runs=runs,
        row_url=runs[0].url if len(runs) == 1 else None,
        measured_at=move.measured_at,
        ago=ago(move.measured_at, now) if move.measured_at else '',
    )


def movement_summary(found: ClassMovement) -> str:
    parts = (
        [
            f'{elo_text(found.net, 1)} Elo chained from {plural(found.measured, "finished measurement")}',
            f'{plural(found.accepted, "step")} accepted',
        ]
        if found.net is not None
        else ['No measurement finished in the window']
    )
    if found.remeasured:
        parts.append(f'{plural(found.remeasured, "re-measured step")} counted with the pooled value of all its runs')
    if found.provisional:
        parts.append(f'{plural(found.provisional, "provisional step")} still running and not counted')
    return ', '.join(parts) + '.'


def class_block(engine_index: int, found: ClassMovement, now: datetime) -> ClassBlock:
    return ClassBlock(
        id=f'digest-trunk-{engine_index}-{found.time_class.value}',
        label=found.time_class.label,
        summary=movement_summary(found),
        lines=[move_line(move, now) for move in reversed(found.moves)],
        omitted=found.moves_omitted,
    )


def trunk_block(index: int, movement: TrunkMovement, now: datetime) -> TrunkBlock:
    return TrunkBlock(
        engine=movement.engine,
        url=progress_url(movement.engine, DEFAULT_WINDOW),
        classes=[class_block(index, found, now) for found in movement.classes],
    )


def bucket_label(hours: int) -> str:
    return 'hour' if hours == 1 else f'{hours} hours'


def bucket_line(bucket: GamesBucket, hours: int) -> BucketLine:
    return BucketLine(bucket.start, [count(games) for games in bucket.games], count(round(bucket.total / hours)))


def hours_note(fleet: FleetActivity) -> str:
    if fleet.core_hours is None:
        return 'No workload played in this window has node counters, so its search time is unknown.'
    parts = [
        'Core-hours are the engines’ own move times from the workers’ node counters, times each side’s thread '
        'count, shared out by the games each workload played in the window: search time, not machine uptime.'
    ]
    if fleet.core_hours_estimated:
        parts.append('Part of it is estimated from workloads whose counters cover only some of their games.')
    if fleet.games_without_hours:
        parts.append(f'{plural(fleet.games_without_hours, "game")} had no counters to estimate from and are left out.')
    return ' '.join(parts)


def fleet_view(fleet: FleetActivity) -> FleetView:
    return FleetView(
        played=fleet.games > 0,
        bucket=bucket_label(fleet.bucket_hours),
        series=[SeriesKey(time_class.label, time_class.value) for time_class in fleet.classes],
        lines=[bucket_line(bucket, fleet.bucket_hours) for bucket in reversed(fleet.buckets) if bucket.total],
        pools=fleet.pools,
        pools_omitted=fleet.pools_omitted,
        hours_note=hours_note(fleet),
    )


def finished_tile(finished: FinishedDigest) -> Tile:
    counts = finished.counts
    meta = f'{count(counts.passed)} passed · {count(counts.failed)} failed'
    if counts.completed:
        meta += f' · {count(counts.completed)} completed'
    if counts.stopped:
        meta += f' · {count(counts.stopped)} stopped'
    return Tile('Finished', count(counts.total), meta, 'pass' if counts.passed else None)


def running_tile(running: RunningDigest) -> Tile:
    meta = f'{count(running.started)} started in the window'
    if running.pending:
        meta += f' · {count(running.pending)} pending'
    return Tile('Still running', count(running.total - running.pending), meta, 'info')


def trunk_tiles(trunk: list[TrunkMovement]) -> Iterator[Tile]:
    several = len(trunk) > 1
    for movement in trunk:
        for found in movement.classes:
            label = (
                f'{movement.engine} {found.time_class.label} trunk' if several else f'{found.time_class.label} trunk'
            )
            meta = f'{plural(found.accepted, "step")} accepted'
            if found.provisional:
                meta += f' · {count(found.provisional)} provisional'
            if found.remeasured:
                meta += f' · {count(found.remeasured)} re-measured'
            if found.net is None:
                yield Tile(label, DASH, meta)
            else:
                yield Tile(label, elo_text(found.net, 1), meta, chained_tone(found.net))


def games_tile(fleet: FleetActivity) -> Tile:
    peak = DASH if fleet.peak_games_per_hour is None else f'peak {count(round(fleet.peak_games_per_hour))} per hour'
    return Tile('Games played', count(fleet.games), peak)


def hours_meta(fleet: FleetActivity) -> str:
    if fleet.games_without_hours:
        return f'excludes {plural(fleet.games_without_hours, "game")}'
    return 'partly estimated' if fleet.core_hours_estimated else 'measured search time'


def hours_tile(fleet: FleetActivity) -> Tile:
    if not fleet.games:
        return Tile('Core-hours', DASH, 'no games played')
    if fleet.core_hours is None:
        return Tile('Core-hours', DASH, 'no node counters')
    value = f'{"≈ " if fleet.core_hours_estimated else ""}{fleet.core_hours:,.1f}'
    return Tile('Core-hours', value, hours_meta(fleet))


def hosts_tile(fleet: FleetActivity) -> Tile:
    return Tile('Hosts active', count(fleet.hosts), plural(len(fleet.pools) + fleet.pools_omitted, 'pool'))


def errors_tile(errors: ErrorDigest) -> Tile:
    meta = f'{plural(errors.total, "group")} seen · {count(errors.new)} new'
    return Tile('Unresolved errors', count(errors.unresolved), meta, 'fail' if errors.unresolved else 'pass')


def tiles(report: DigestReport) -> list[Tile]:
    return [
        finished_tile(report.finished),
        running_tile(report.running),
        *trunk_tiles(report.trunk),
        games_tile(report.fleet),
        hours_tile(report.fleet),
        hosts_tile(report.fleet),
        errors_tile(report.errors),
    ]


def digest_page(report: DigestReport) -> DigestPage:
    now = report.generated_at
    return DigestPage(
        generated_at=now,
        window=report.window,
        span=span_phrase(report.window),
        options=window_options(report.window),
        headline=report.headline,
        tiles=tiles(report),
        finished=[finished_line(row, now) for row in report.finished.workloads],
        finished_omitted=report.finished.omitted,
        running=[running_line(row, now) for row in report.running.workloads],
        running_omitted=report.running.omitted,
        trunk=[trunk_block(index, movement, now) for index, movement in enumerate(report.trunk)],
        fleet=fleet_view(report.fleet),
        errors=report.errors,
        errors_url=UNRESOLVED_ERRORS_URL if report.errors.unresolved else ERRORS_URL,
    )
