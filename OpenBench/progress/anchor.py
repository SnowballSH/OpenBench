from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from functools import reduce

from OpenBench.progress.domain import (
    ANCHOR_BRANCHES_SENT,
    ANCHOR_POINTS_SENT,
    AnchorPoint,
    AnchorSeries,
    Measurement,
    ReleaseReport,
    RunMode,
    RunRow,
    Step,
    TimeClass,
)
from OpenBench.progress.lineage import build_steps
from OpenBench.releases.domain import ReleaseAnchor, same_repository

type BranchCommits = Mapping[str, datetime | None]


def anchor_runs(rows: Iterable[RunRow], anchor: ReleaseAnchor) -> list[RunRow]:
    sha = anchor.sha.lower()
    return [row for row in rows if row.base.sha.lower() == sha and row.dev.sha.lower() != sha]


def built_from_default_branch(rows: Iterable[RunRow], anchor: ReleaseAnchor, source: str) -> set[str]:
    # A test created from the branch's own name built the branch's head at that moment
    return {
        row.dev.sha
        for row in rows
        if anchor.default_branch and row.dev_name == anchor.default_branch and same_repository(row.repo, source)
    }


def first_use_as_base(rows: Iterable[RunRow]) -> dict[str, datetime]:
    first: dict[str, datetime] = {}
    for row in rows:
        known = first.get(row.base.sha)
        first[row.base.sha] = row.created_at if known is None else min(known, row.created_at)
    return first


def newer_bases(step: Step, first_use: Mapping[str, datetime]) -> int:
    measured = {step.base.sha, step.dev.sha}
    return sum(sha not in measured and used > step.first_tested_at for sha, used in first_use.items())


def measured_at(measurement: Measurement) -> datetime:
    return max(run.finished_at or run.created_at for run in measurement.runs)


def anchor_points(step: Step, committed_at: datetime | None, moved: int) -> list[AnchorPoint]:
    return [
        AnchorPoint(
            time_class=measurement.time_class,
            base=step.base,
            dev=step.dev,
            repo=step.repo,
            subject=step.subject,
            committed_at=committed_at,
            measured_at=measured_at(measurement),
            first_run=step.first_run,
            newer_bases=moved,
            measurement=measurement,
        )
        for measurement in step.measurements
    ]


def chronological(points: Iterable[AnchorPoint]) -> list[AnchorPoint]:
    return sorted(points, key=lambda point: (point.measured_at, point.first_run))


def newer(candidate: AnchorPoint, current: AnchorPoint) -> bool:
    # Commit order when GitHub dated both commits; otherwise only the order they were measured in is known
    if candidate.committed_at is not None and current.committed_at is not None:
        return (candidate.committed_at, candidate.measured_at) > (current.committed_at, current.measured_at)
    return candidate.measured_at >= current.measured_at


def headline(points: Sequence[AnchorPoint]) -> AnchorPoint | None:
    settled = [point for point in points if not point.measurement.provisional and point.measurement.elo is not None]
    return reduce(lambda current, point: point if newer(point, current) else current, settled) if settled else None


def anchor_series(points: Iterable[AnchorPoint]) -> list[AnchorSeries]:
    by_class: defaultdict[TimeClass, list[AnchorPoint]] = defaultdict(list)
    for point in chronological(points):
        by_class[point.time_class].append(point)
    return [
        AnchorSeries(time_class, headline(found), found[-ANCHOR_POINTS_SENT:])
        for time_class in TimeClass
        if (found := by_class.get(time_class))
    ]


def release_report(
    engine: str,
    anchor: ReleaseAnchor | None,
    rows: Sequence[RunRow],
    branch_commits: BranchCommits,
    source: str,
) -> ReleaseReport:
    if anchor is None or not anchor.known:
        return ReleaseReport(engine, source, anchor, [], [])

    against_release = anchor_runs(rows, anchor)
    measured = [row for row in against_release if row.mode == RunMode.GAMES]
    stopped_early = [row for row in against_release if row.mode == RunMode.SPRT]
    on_branch = set(branch_commits) | built_from_default_branch(measured, anchor, source)
    first_use = first_use_as_base(rows)

    mainline: list[AnchorPoint] = []
    branches: list[AnchorPoint] = []
    for step in build_steps(measured):
        points = anchor_points(step, branch_commits.get(step.dev.sha), newer_bases(step, first_use))
        (mainline if step.dev.sha in on_branch else branches).extend(points)
    sprt = [
        point
        for step in build_steps(stopped_early)
        for point in anchor_points(step, branch_commits.get(step.dev.sha), newer_bases(step, first_use))
    ]

    series = anchor_series(mainline)
    newest_branches = chronological(branches)[::-1]
    newest_sprt = chronological(sprt)[::-1]
    return ReleaseReport(
        engine=engine,
        repo=source,
        anchor=anchor,
        series=series,
        branches=newest_branches[:ANCHOR_BRANCHES_SENT],
        sprt=newest_sprt[:ANCHOR_BRANCHES_SENT],
        points_omitted=len(mainline) - sum(len(found.points) for found in series),
        branches_omitted=max(0, len(newest_branches) - ANCHOR_BRANCHES_SENT),
        sprt_omitted=max(0, len(newest_sprt) - ANCHOR_BRANCHES_SENT),
    )
