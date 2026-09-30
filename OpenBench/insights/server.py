from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from django.db.models import Max, Min
from django.utils import timezone

from OpenBench.insights.domain import WorkloadMode, spsa_target_games, tune_completed
from OpenBench.models import Machine, Profile, Test, WorkloadSnapshot

ACTIVE_MACHINE = timedelta(minutes=2)
GAMES_WINDOW = timedelta(hours=24)
FINISHED_WINDOW = timedelta(days=7)
TOP_CONTRIBUTORS = 10


@dataclass(frozen=True, slots=True)
class FleetStatus:
    machines: int
    threads: int
    mnps: float


@dataclass(frozen=True, slots=True)
class WorkloadCounts:
    pending: int
    active: int


@dataclass(frozen=True, slots=True)
class FinishedSummary:
    window_days: int
    total: int
    passed: int
    failed: int
    completed: int
    stopped: int
    sprt_passed: int
    sprt_failed: int
    sprt_pass_rate: float | None


@dataclass(frozen=True, slots=True)
class Contributor:
    username: str
    games: int


@dataclass(frozen=True, slots=True)
class ServerInsights:
    generated_at: datetime
    fleet: FleetStatus
    workloads: WorkloadCounts
    games_last_24h: int
    finished_last_7d: FinishedSummary
    top_contributors: list[Contributor]


@dataclass(frozen=True, slots=True)
class Edge:
    created: datetime
    games: int


@dataclass(frozen=True, slots=True)
class FinishedWorkload:
    mode: WorkloadMode
    passed: bool
    failed: bool
    completed: bool = False


def fleet_status(machines: Iterable[tuple[dict[str, Any], float]]) -> FleetStatus:
    rows = [(int(info.get('concurrency', 0)), mnps) for info, mnps in machines]
    return FleetStatus(
        machines=len(rows),
        threads=sum(threads for threads, _ in rows),
        mnps=sum(threads * mnps for threads, mnps in rows),
    )


def games_at_window_start(since: datetime, before: Edge | None, after: Edge) -> float:

    if before is None:
        return 0.0

    if after.created == before.created:
        return float(after.games)

    return before.games + (after.games - before.games) * ((since - before.created) / (after.created - before.created))


def games_in_window(
    since: datetime, before: Mapping[int, Edge], after: Mapping[int, Edge], current: Mapping[int, int]
) -> int:
    return round(
        sum(
            max(0.0, current.get(test_id, edge.games) - games_at_window_start(since, before.get(test_id), edge))
            for test_id, edge in after.items()
        )
    )


def summarize_finished(workloads: Iterable[FinishedWorkload], window: timedelta) -> FinishedSummary:

    items = list(workloads)
    sprt = [item for item in items if item.mode == WorkloadMode.SPRT]
    passed = sum(item.passed for item in sprt)
    failed = sum(item.failed for item in sprt)

    return FinishedSummary(
        window_days=window.days,
        total=len(items),
        passed=sum(item.passed for item in items),
        failed=sum(item.failed for item in items),
        completed=sum(item.completed and not (item.passed or item.failed) for item in items),
        stopped=sum(not (item.passed or item.failed or item.completed) for item in items),
        sprt_passed=passed,
        sprt_failed=failed,
        sprt_pass_rate=passed / (passed + failed) if passed + failed else None,
    )


def load_fleet(now: datetime) -> FleetStatus:
    return fleet_status(Machine.objects.filter(updated__gte=now - ACTIVE_MACHINE).values_list('info', 'mnps'))


def load_workload_counts() -> WorkloadCounts:
    unfinished = Test.objects.filter(finished=False, deleted=False)
    return WorkloadCounts(
        pending=unfinished.filter(approved=False).count(),
        active=unfinished.filter(approved=True).count(),
    )


def load_games_since(since: datetime) -> int:

    recent = WorkloadSnapshot.objects.filter(created__gte=since).values('test_id')
    after = {
        row['test_id']: Edge(row['edge_at'], row['edge_games'])
        for row in recent.annotate(edge_at=Min('created'), edge_games=Min('games'))
    }

    earlier = WorkloadSnapshot.objects.filter(test_id__in=list(after), created__lt=since).values('test_id')
    before = {
        row['test_id']: Edge(row['edge_at'], row['edge_games'])
        for row in earlier.annotate(edge_at=Max('created'), edge_games=Max('games'))
    }

    current = dict(Test.objects.filter(id__in=list(after)).values_list('id', 'games'))
    return games_in_window(since, before, after, current)


def load_finished_since(since: datetime) -> list[FinishedWorkload]:
    rows = Test.objects.filter(finished=True, deleted=False, updated__gte=since).values_list(
        'test_mode', 'passed', 'failed', 'games', 'spsa_run__pairs_per', 'spsa_run__iterations'
    )
    return [
        FinishedWorkload(
            WorkloadMode(mode),
            passed,
            failed,
            tune_completed(
                WorkloadMode(mode), games, spsa_target_games(pairs_per, iterations) if pairs_per is not None else None
            ),
        )
        for mode, passed, failed, games, pairs_per, iterations in rows
    ]


def load_top_contributors(limit: int = TOP_CONTRIBUTORS) -> list[Contributor]:
    rows = (
        Profile.objects.filter(games__gt=0)
        .order_by('-games', 'user__username')
        .values_list('user__username', 'games')[:limit]
    )
    return [Contributor(username, games) for username, games in rows]


def server_insights(now: datetime | None = None) -> ServerInsights:
    now = now or timezone.now()
    return ServerInsights(
        generated_at=now,
        fleet=load_fleet(now),
        workloads=load_workload_counts(),
        games_last_24h=load_games_since(now - GAMES_WINDOW),
        finished_last_7d=summarize_finished(load_finished_since(now - FINISHED_WINDOW), FINISHED_WINDOW),
        top_contributors=load_top_contributors(),
    )
