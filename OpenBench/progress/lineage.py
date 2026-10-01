import math
from collections import defaultdict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime

from OpenBench.insights.strength import EloInterval, elo_interval, score_moments
from OpenBench.listing_rows import split_info
from OpenBench.progress.domain import (
    CANDIDATES_SENT,
    DETACHED_SENT,
    MINIMUM_SAMPLE,
    TRUNK_STEPS_SENT,
    Candidate,
    ChainPoint,
    ChainSeries,
    Commit,
    DirectCheck,
    DirectRun,
    Lineage,
    LineageReport,
    LineageSummary,
    Measurement,
    OtherLineage,
    Pooling,
    Run,
    RunRow,
    RunStatus,
    Step,
    StepCost,
    TimeClass,
    TrunkStep,
)
from OpenBench.progress.speed import core_hours, has_counters, step_speed

type Edge = tuple[Commit, Commit]
type Children = Mapping[Commit, Sequence[Step]]

UNDECIDED_ORDER = (RunStatus.RUNNING, RunStatus.PENDING, RunStatus.COMPLETED, RunStatus.STOPPED)
DECIDED = (RunStatus.PASSED, RunStatus.FAILED)
UNFINISHED = (RunStatus.RUNNING, RunStatus.PENDING)


def estimate(results: Sequence[int]) -> EloInterval | None:
    moments = score_moments(results)
    if moments is None or moments.count < MINIMUM_SAMPLE or moments.variance == 0.0:
        return None
    return elo_interval(results)


def run_of(row: RunRow) -> Run:
    return Run(
        id=row.id,
        mode=row.mode,
        status=row.status,
        time_control=row.time_control,
        created_at=row.created_at,
        finished_at=row.finished_at,
        games=row.games,
        elo=estimate(row.outcomes.primary()),
        started_at=row.started_at,
        counted_games=sum(host.counted_games for host in row.hosts if has_counters(host)),
        core_hours=core_hours(row),
    )


def pooled_verdict(rows: Sequence[RunRow]) -> RunStatus:
    decided = [row for row in rows if row.status in DECIDED]
    if decided:
        return max(decided, key=lambda row: (row.created_at, row.id)).status
    present = {row.status for row in rows}
    return next(status for status in UNDECIDED_ORDER if status in present)


def pool(time_class: TimeClass, rows: Sequence[RunRow]) -> Measurement:
    ordered = sorted(rows, key=lambda row: (row.created_at, row.id))
    finished = [row for row in ordered if row.status not in UNFINISHED]
    pooled = finished or ordered
    use_penta = all(row.outcomes.use_penta for row in pooled)
    counts = [row.outcomes.pentanomial if use_penta else row.outcomes.trinomial for row in pooled]
    return Measurement(
        time_class=time_class,
        verdict=pooled_verdict(ordered),
        games=sum(row.games for row in pooled),
        pooling=Pooling.PENTANOMIAL if use_penta else Pooling.TRINOMIAL,
        provisional=not finished,
        elo=estimate([sum(column) for column in zip(*counts, strict=True)]),
        runs=[run_of(row) for row in ordered],
    )


def last_activity(row: RunRow) -> datetime:
    return row.finished_at or row.created_at


def decision_seconds(row: RunRow) -> float | None:
    if row.finished_at is None:
        return None
    return max(0.0, (row.finished_at - (row.started_at or row.created_at)).total_seconds())


def step_cost(rows: Sequence[RunRow]) -> StepCost:
    durations = [found for row in rows if (found := decision_seconds(row)) is not None]
    spent = [found for row in rows if (found := core_hours(row)) is not None]
    return StepCost(
        runs=len(rows),
        games=sum(row.games for row in rows),
        decision_seconds=sum(durations) if durations else None,
        core_hours=sum(spent) if spent else None,
        counted_games=sum(host.counted_games for row in rows for host in row.hosts if has_counters(host)),
    )


def step_of(rows: Sequence[RunRow]) -> Step:
    first = min(rows, key=lambda row: row.id)
    subjects = (split_info(row.subject)[0] for row in sorted(rows, key=lambda row: row.id))
    by_class: defaultdict[TimeClass, list[RunRow]] = defaultdict(list)
    for row in rows:
        by_class[row.time_class].append(row)
    return Step(
        base=first.base,
        dev=first.dev,
        repo=first.repo,
        subject=next((subject for subject in subjects if subject), ''),
        author=first.author,
        first_run=first.id,
        first_tested_at=min(row.created_at for row in rows),
        last_tested_at=max(row.created_at for row in rows),
        measured_at=max(last_activity(row) for row in rows),
        measurements=[pool(time_class, by_class[time_class]) for time_class in TimeClass if time_class in by_class],
        base_bench=first.base_bench or None,
        dev_bench=first.dev_bench or None,
        speed=step_speed(rows),
        cost=step_cost(rows),
    )


def build_steps(rows: Iterable[RunRow]) -> list[Step]:
    pairs: defaultdict[Edge, list[RunRow]] = defaultdict(list)
    for row in rows:
        if row.base != row.dev:
            pairs[row.base, row.dev].append(row)
    return sorted((step_of(found) for found in pairs.values()), key=lambda step: step.first_run)


def edge(step: Step) -> Edge:
    return step.base, step.dev


def children_of(steps: Iterable[Step]) -> dict[Commit, list[Step]]:
    children: defaultdict[Commit, list[Step]] = defaultdict(list)
    for step in steps:
        children[step.base].append(step)
    return dict(children)


def passed_unopposed(step: Step) -> bool:
    verdicts = {measurement.verdict for measurement in step.measurements}
    return RunStatus.PASSED in verdicts and RunStatus.FAILED not in verdicts


def keep_order(step: Step) -> tuple[bool, int]:
    return not passed_unopposed(step), step.first_run


def topological(steps: Sequence[Step], children: Children) -> tuple[list[Commit], set[Edge]]:
    finished: dict[Commit, bool] = {}
    postorder: list[Commit] = []
    cyclic: set[Edge] = set()

    for start in (commit for step in steps for commit in edge(step)):
        if start in finished:
            continue
        finished[start] = False
        stack: list[tuple[Commit, Iterator[Step]]] = [(start, iter(children.get(start, ())))]
        while stack:
            node, pending = stack[-1]
            for step in pending:
                if step.dev not in finished:
                    finished[step.dev] = False
                    stack.append((step.dev, iter(children.get(step.dev, ()))))
                    break
                if not finished[step.dev]:
                    cyclic.add(edge(step))
            else:
                finished[node] = True
                postorder.append(node)
                stack.pop()

    return postorder[::-1], cyclic


def primary_steps(steps: Sequence[Step]) -> dict[Commit, Step]:
    preferred = sorted(steps, key=keep_order)
    children = children_of(preferred)
    order, cyclic = topological(preferred, children)
    depth: dict[Commit, int] = {}
    primary: dict[Commit, Step] = {}

    for node in order:
        reached = depth.get(node, 0) + 1
        for step in children.get(node, ()):
            if edge(step) in cyclic:
                continue
            current = primary.get(step.dev)
            if current is None or (reached, -step.first_run) > (depth[step.dev], -current.first_run):
                depth[step.dev], primary[step.dev] = reached, step

    return primary


def taken(step: Step, children: Children) -> bool:
    return step.dev in children or passed_unopposed(step)


def descendants(node: Commit, children: Children) -> list[Step]:
    found: list[Step] = []
    stack = list(reversed(children.get(node, ())))
    while stack:
        step = stack.pop()
        found.append(step)
        stack.extend(reversed(children.get(step.dev, ())))
    return found


@dataclass(frozen=True, slots=True)
class Tree:
    root: Commit
    steps: list[Step]
    taken: int

    @property
    def rank(self) -> tuple[int, int, datetime]:
        games = sum(measurement.games for step in self.steps for measurement in step.measurements)
        return self.taken, games, max(step.last_tested_at for step in self.steps)


def trees(forest: Sequence[Step], primary: Mapping[Commit, Step], children: Children) -> list[Tree]:
    roots = dict.fromkeys(step.base for step in forest if step.base not in primary)
    return [
        Tree(root, steps, sum(taken(step, children) for step in steps))
        for root in roots
        for steps in [descendants(root, children)]
    ]


def subtree_activity(tree: Tree) -> dict[Commit, datetime]:
    activity: dict[Commit, datetime] = {}
    for step in reversed(tree.steps):
        newest = max(step.last_tested_at, activity.get(step.dev, step.last_tested_at))
        activity[step.dev] = newest
        activity[step.base] = max(newest, activity.get(step.base, newest))
    return activity


def find_head(tree: Tree, children: Children) -> Commit:
    activity = subtree_activity(tree)
    head = tree.root
    while options := [step for step in children.get(head, ()) if taken(step, children)]:
        continued = [step for step in options if step.dev in children]
        head = max(continued or options, key=lambda step: (activity[step.dev], step.first_run)).dev
    return head


def trunk_to(head: Commit, primary: Mapping[Commit, Step]) -> list[Step]:
    trunk: list[Step] = []
    node = head
    while (step := primary.get(node)) is not None:
        trunk.append(step)
        node = step.base
    return trunk[::-1]


def branch(node: Commit, children: Children, on_trunk: set[Edge]) -> list[Candidate]:
    found: list[Candidate] = []
    stack = [(1, step) for step in reversed(children.get(node, ())) if edge(step) not in on_trunk]
    while stack:
        depth, step = stack.pop()
        found.append(Candidate(depth, step))
        stack.extend((depth + 1, child) for child in reversed(children.get(step.dev, ())))
    return found


def build_lineage(steps: Sequence[Step]) -> Lineage | None:
    if not steps:
        return None

    primary = primary_steps(steps)
    forest = sorted(primary.values(), key=lambda step: step.first_run)
    children = children_of(forest)
    ranked = sorted(trees(forest, primary, children), key=lambda tree: tree.rank, reverse=True)
    chosen = ranked[0]
    head = find_head(chosen, children)
    trunk = trunk_to(head, primary)

    on_trunk = {edge(step) for step in trunk}
    position = {node: index for index, node in enumerate([chosen.root, *(step.dev for step in trunk)])}
    branches = {node: found for node in position if (found := branch(node, children, on_trunk))}

    in_forest = {edge(step) for step in forest}
    placed = on_trunk | {edge(found.step) for candidates in branches.values() for found in candidates}
    direct = [
        DirectRun(position[step.base] + 1, position[step.dev], step)
        for step in steps
        if edge(step) not in in_forest and position.get(step.base, len(position)) < position.get(step.dev, -1)
    ]
    placed |= {edge(run.step) for run in direct}

    return Lineage(
        root=chosen.root,
        head=head,
        trunk=trunk,
        branches=branches,
        direct=direct,
        detached=[step for step in steps if edge(step) not in placed],
        others=[OtherLineage(tree.root, len(tree.steps), tree.taken) for tree in ranked[1:] if tree.taken],
    )


def half_width(interval: EloInterval) -> float:
    return (interval.upper - interval.lower) / 2


@dataclass(slots=True)
class RunningChain:
    count: int = 0
    value: float = 0.0
    variance: float = 0.0

    def add(self, interval: EloInterval) -> None:
        self.count += 1
        self.value += interval.value
        self.variance += half_width(interval) ** 2

    def interval(self, extra: EloInterval | None = None) -> EloInterval | None:
        if extra is None and not self.count:
            return None
        value = self.value + (extra.value if extra else 0.0)
        margin = math.sqrt(self.variance + (half_width(extra) ** 2 if extra else 0.0))
        return EloInterval(value - margin, value, value + margin)


def chain(estimates: Iterable[EloInterval]) -> EloInterval | None:
    running = RunningChain()
    for interval in estimates:
        running.add(interval)
    return running.interval()


def step_estimate(step: Step, time_class: TimeClass) -> EloInterval | None:
    measurement = step.measurement(time_class)
    return measurement.elo if measurement and not measurement.provisional else None


def chain_point(index: int, step: Step, time_class: TimeClass, running: RunningChain) -> ChainPoint:
    measurement = step.measurement(time_class)
    if measurement is None or measurement.elo is None:
        return ChainPoint(index, None, None, None)
    if measurement.provisional:
        return ChainPoint(index, measurement.elo, None, running.interval(measurement.elo))
    running.add(measurement.elo)
    return ChainPoint(index, measurement.elo, running.interval(), None)


def chain_series(steps: Sequence[Step], time_class: TimeClass, first_index: int) -> ChainSeries:
    running = RunningChain()
    points = [chain_point(index, step, time_class, running) for index, step in enumerate(steps, start=first_index)]
    return ChainSeries(
        time_class=time_class,
        points=points,
        total=running.interval(),
        measured=running.count,
        provisional=sum(point.projected is not None for point in points),
        steps=len(steps),
    )


def window_offset(trunk: Sequence[Step], since: datetime | None) -> int:
    if since is None:
        return 0
    stale = (index for index in range(len(trunk), 0, -1) if trunk[index - 1].measured_at < since)
    return next(stale, 0)


def direct_checks(lineage: Lineage, offset: int) -> list[DirectCheck]:
    checks: list[DirectCheck] = []
    for run in lineage.direct:
        if run.last_index <= offset:
            continue
        spanned = lineage.trunk[run.first_index - 1 : run.last_index]
        for measurement in run.step.measurements:
            if not measurement.time_class.chained:
                continue
            estimates = [found for step in spanned if (found := step_estimate(step, measurement.time_class))]
            checks.append(
                DirectCheck(
                    time_class=measurement.time_class,
                    base=run.step.base,
                    dev=run.step.dev,
                    repo=run.step.repo,
                    first_index=run.first_index,
                    last_index=run.last_index,
                    direct=measurement,
                    chained=chain(estimates),
                    measured=len(estimates),
                    steps=len(spanned),
                )
            )
    return checks


def newest(candidates: Sequence[Candidate], limit: int) -> list[Candidate]:
    if len(candidates) <= limit:
        return list(candidates)
    kept = sorted(candidates, key=lambda found: found.step.last_tested_at)[-limit:]
    keep = {edge(found.step) for found in kept}
    return [found for found in candidates if edge(found.step) in keep]


def classes_measured(steps: Iterable[Step]) -> set[TimeClass]:
    return {measurement.time_class for step in steps for measurement in step.measurements}


def lineage_report(engine: str, lineage: Lineage, since: datetime | None) -> LineageReport:
    offset = window_offset(lineage.trunk, since)
    shown = lineage.trunk[offset:]
    origin = shown[0].base if shown else lineage.head
    origin_candidates = lineage.branches.get(origin, [])
    sent = shown[-TRUNK_STEPS_SENT:]
    omitted = len(shown) - len(sent)

    trunk_steps = [
        TrunkStep(
            index=index,
            step=step,
            candidates=newest(candidates, CANDIDATES_SENT),
            candidates_omitted=max(0, len(candidates) - CANDIDATES_SENT),
        )
        for index, step in enumerate(sent, start=offset + omitted + 1)
        for candidates in [lineage.branches.get(step.dev, [])]
    ]

    on_trunk = classes_measured(shown)
    listed = (
        on_trunk
        | classes_measured(found.step for row in trunk_steps for found in row.candidates)
        | classes_measured(found.step for found in origin_candidates)
    )
    series = [
        replace(full, points=full.points[omitted:])
        for time_class in TimeClass
        if time_class in on_trunk and time_class.chained
        for full in [chain_series(shown, time_class, offset + 1)]
    ]
    detached = sorted(lineage.detached, key=lambda step: (step.last_tested_at, step.first_run), reverse=True)

    return LineageReport(
        engine=engine,
        classes=[time_class for time_class in TimeClass if time_class in listed] or [TimeClass.STC],
        origin=origin,
        origin_candidates=newest(origin_candidates, CANDIDATES_SENT),
        origin_candidates_omitted=max(0, len(origin_candidates) - CANDIDATES_SENT),
        head=lineage.head,
        trunk_length=len(lineage.trunk),
        steps=trunk_steps,
        steps_omitted=omitted,
        series=series,
        direct=direct_checks(lineage, offset),
        detached=detached[:DETACHED_SENT],
        detached_omitted=max(0, len(detached) - DETACHED_SENT),
        others=lineage.others,
    )


def summarize_lineage(lineage: Lineage, since: datetime | None) -> LineageSummary:
    shown = lineage.trunk[window_offset(lineage.trunk, since) :]
    origin = shown[0].base if shown else lineage.head
    nodes = [origin, *(step.dev for step in shown)]
    candidates = [found.step for node in nodes for found in lineage.branches.get(node, [])]
    measurements = [measurement for step in [*shown, *candidates] for measurement in step.measurements]
    return LineageSummary(
        trunk_steps=len(shown),
        candidates=len(candidates),
        measurements=len(measurements),
        runs=sum(len(measurement.runs) for measurement in measurements),
    )
