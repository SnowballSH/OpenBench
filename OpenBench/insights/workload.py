from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from django.utils import timezone

from OpenBench.insights.contributions import Contributions, ResultRow, summarize_contributions
from OpenBench.insights.domain import Pentanomial, ProgressPoint, Trinomial, WorkloadFacts, WorkloadMode, WorkloadStatus
from OpenBench.insights.eta import Eta, timing_and_eta
from OpenBench.insights.series import SeriesPoint, build_series
from OpenBench.insights.sources import result_rows, snapshot_points, workload_facts
from OpenBench.insights.strength import StrengthSummary, summarize_strength
from OpenBench.insights.timing import Mark, TimingSummary, timeline_marks
from OpenBench.models import Test


@dataclass(frozen=True, slots=True)
class WorkloadSummary:
    id: int
    mode: WorkloadMode
    status: WorkloadStatus
    use_penta: bool
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class Progress:
    games: int
    pairs: int
    trinomial: Trinomial
    pentanomial: Pentanomial
    llr: float | None
    llr_lower: float | None
    llr_upper: float | None
    target_games: int | None
    fraction: float | None


@dataclass(frozen=True, slots=True)
class History:
    synthetic: bool
    points: list[SeriesPoint]


@dataclass(frozen=True, slots=True)
class WorkloadInsights:
    generated_at: datetime
    workload: WorkloadSummary
    progress: Progress
    timing: TimingSummary | None
    eta: Eta
    strength: StrengthSummary | None
    history: History
    contributions: Contributions


def current_point(facts: WorkloadFacts) -> ProgressPoint:
    return ProgressPoint(facts.updated_at, facts.outcomes.games, facts.outcomes, facts.llr)


def with_current(facts: WorkloadFacts, snapshots: Sequence[ProgressPoint]) -> list[ProgressPoint]:

    if not snapshots:
        return [current_point(facts)] if facts.outcomes.games else []

    if facts.outcomes.games <= snapshots[-1].games:
        return list(snapshots)

    tail = current_point(facts)
    return [
        *snapshots,
        ProgressPoint(max(tail.timestamp, snapshots[-1].timestamp), tail.games, tail.outcomes, tail.llr),
    ]


def current_mark(facts: WorkloadFacts) -> Mark:
    return facts.updated_at, facts.outcomes.games


def timeline(facts: WorkloadFacts, snapshots: Sequence[ProgressPoint]) -> list[Mark]:
    return timeline_marks(
        facts.created_at, current_mark(facts), [(point.timestamp, point.games) for point in snapshots]
    )


def summarize_progress(facts: WorkloadFacts) -> Progress:
    target = facts.target_games
    is_sprt = facts.sprt is not None
    return Progress(
        games=facts.outcomes.games,
        pairs=facts.outcomes.pairs,
        trinomial=facts.outcomes.trinomial,
        pentanomial=facts.outcomes.pentanomial,
        llr=facts.llr if is_sprt else None,
        llr_lower=facts.sprt.lower_llr if facts.sprt else None,
        llr_upper=facts.sprt.upper_llr if facts.sprt else None,
        target_games=target,
        fraction=min(1.0, facts.outcomes.games / target) if target else None,
    )


def has_elo(facts: WorkloadFacts) -> bool:
    return facts.mode != WorkloadMode.SPSA


def build_history(facts: WorkloadFacts, snapshots: Sequence[ProgressPoint]) -> History:
    points = with_current(facts, snapshots)
    return History(
        synthetic=bool(points) and not snapshots,
        points=build_series(points, facts.sprt is not None, has_elo(facts)),
    )


def build_insights(
    facts: WorkloadFacts, snapshots: Sequence[ProgressPoint], rows: Sequence[ResultRow], now: datetime
) -> WorkloadInsights:

    timing, eta = timing_and_eta(facts, timeline(facts, snapshots), now)
    elapsed = timing.elapsed_seconds if timing else None

    return WorkloadInsights(
        generated_at=now,
        workload=WorkloadSummary(
            facts.id, facts.mode, facts.status, facts.outcomes.use_penta, facts.created_at, facts.updated_at
        ),
        progress=summarize_progress(facts),
        timing=timing,
        eta=eta,
        strength=summarize_strength(facts.outcomes) if has_elo(facts) else None,
        history=build_history(facts, snapshots),
        contributions=summarize_contributions(rows, facts.outcomes.use_penta, elapsed),
    )


def workload_insights(test: Test, now: datetime | None = None) -> WorkloadInsights:
    return build_insights(workload_facts(test), snapshot_points(test), result_rows(test), now or timezone.now())


def insights_without_contributions(test: Test, now: datetime | None = None) -> WorkloadInsights:
    return build_insights(workload_facts(test), snapshot_points(test), (), now or timezone.now())


def workload_history(test: Test) -> History:
    return build_history(workload_facts(test), snapshot_points(test))
