from dataclasses import dataclass
from datetime import datetime
from typing import Any

from django.db.models import Case, Count, IntegerField, Max, Min, OuterRef, Subquery, Sum, Value, When
from django.db.models.fields.json import KT
from django.db.models.functions import Coalesce

from OpenBench.fleet.hosts import HostKey
from OpenBench.fleet.machines import load_workloads
from OpenBench.fleet.sessions import threads_of
from OpenBench.fleet.status import UNKNOWN, Presence, presence, relative_age
from OpenBench.insights.domain import Outcomes, WorkloadMode
from OpenBench.insights.sources import PENTANOMIAL_FIELDS, TRINOMIAL_FIELDS, outcomes_of_row
from OpenBench.insights.speed import nodes_per_second
from OpenBench.insights.strength import EloInterval, elo_interval
from OpenBench.machine_info import int_of, text_of
from OpenBench.models import Machine, Result, Test

COUNTERS = (*TRINOMIAL_FIELDS, *PENTANOMIAL_FIELDS)
CONTRIBUTION_LIMIT = 50
SESSION_LIMIT = 25


@dataclass(frozen=True, slots=True)
class WorkloadContribution:
    test: Test
    games: int
    sessions: int
    elo: EloInterval | None
    nps: int
    updated: datetime
    updated_ago: str

    @property
    def elo_margin(self) -> float | None:
        return (self.elo.upper - self.elo.lower) / 2 if self.elo else None


@dataclass(frozen=True, slots=True)
class HostSession:
    machine_id: int
    threads: int
    cli_options: str | None
    workload: Test | None
    games: int
    last_seen: datetime
    last_seen_ago: str
    presence: Presence
    selected: bool

    @property
    def online(self) -> bool:
        return self.presence == Presence.ONLINE


@dataclass(frozen=True, slots=True)
class HostSummary:
    key: HostKey
    name: str | None
    owner: str
    cpu_name: str
    isa_name: str | None
    os_name: str | None
    threads: int
    mnps: float
    first_seen: datetime
    last_seen: datetime
    last_seen_ago: str
    presence: Presence
    workload: Test | None
    lifetime_games: int

    @property
    def online(self) -> bool:
        return self.presence == Presence.ONLINE

    @property
    def total_mnps(self) -> float:
        return self.threads * self.mnps


@dataclass(frozen=True, slots=True)
class HostDetail:
    selected: Machine
    current: Machine
    host: HostSummary
    sessions_total: int
    sessions: list[HostSession]
    workloads_total: int
    contributions: list[WorkloadContribution]

    @property
    def truncated(self) -> bool:
        return self.workloads_total > len(self.contributions)

    @property
    def sessions_truncated(self) -> bool:
        return self.sessions_total > len(self.sessions)


def pooled_elo(test: Test, outcomes: Outcomes) -> EloInterval | None:
    if test.test_mode == WorkloadMode.SPSA:
        return None
    return elo_interval(outcomes.primary())


def pooled_outcomes(row: dict[str, Any], use_penta: bool) -> Outcomes:
    return outcomes_of_row({field: row[f'total_{field}'] for field in COUNTERS}, use_penta)


def workload_contribution(test: Test, row: dict[str, Any], now: datetime) -> WorkloadContribution:
    return WorkloadContribution(
        test=test,
        games=row['played'],
        sessions=row['sessions'],
        elo=pooled_elo(test, pooled_outcomes(row, not test.use_tri)),
        nps=nodes_per_second(row['nodes'], row['time']),
        updated=row['latest'],
        updated_ago=relative_age(now - row['latest']),
    )


def host_session(row: dict[str, Any], workloads: dict[int, Test], selected: int, now: datetime) -> HostSession:
    return HostSession(
        machine_id=row['id'],
        threads=row['threads'] or 0,
        cli_options=row['cli_options'] or None,
        workload=workloads.get(row['workload']),
        games=row['games'],
        last_seen=row['updated'],
        last_seen_ago=relative_age(now - row['updated']),
        presence=presence(row['updated'], now),
        selected=row['id'] == selected,
    )


def host_summary(
    current: Machine, first_seen: datetime, lifetime_games: int, workloads: dict[int, Test], now: datetime
) -> HostSummary:
    info = current.info
    return HostSummary(
        key=HostKey(current.host_key),
        name=text_of(info, 'machine_name'),
        owner=current.user.username,
        cpu_name=text_of(info, 'cpu_name') or UNKNOWN,
        isa_name=text_of(info, 'isa_name'),
        os_name=text_of(info, 'os_name'),
        threads=int_of(info, 'concurrency'),
        mnps=current.mnps,
        first_seen=first_seen,
        last_seen=current.updated,
        last_seen_ago=relative_age(now - current.updated),
        presence=presence(current.updated, now),
        workload=workloads.get(current.workload),
        lifetime_games=lifetime_games,
    )


def games_of_session() -> Coalesce:
    played = Result.objects.filter(machine=OuterRef('pk')).values('machine').annotate(total=Sum('games'))
    return Coalesce(Subquery(played.values('total')), 0)


def load_sessions(key: str, selected: int, limit: int) -> list[dict[str, Any]]:
    pinned = Case(When(id=selected, then=Value(1)), default=Value(0), output_field=IntegerField())
    rows = (
        Machine.objects.filter(host_key=key)
        .annotate(pinned=pinned, games=games_of_session())
        .order_by('-pinned', '-updated', '-id')
        .values('id', 'updated', 'workload', 'games', threads=threads_of(), cli_options=KT('info__cli_options'))
    )
    return sorted((dict(row) for row in rows[:limit]), key=lambda row: (row['updated'], row['id']), reverse=True)


def load_contributions(key: str, limit: int) -> list[dict[str, Any]]:
    rows = (
        Result.objects.filter(machine__host_key=key)
        .order_by()
        .values('test_id')
        .annotate(
            played=Sum('games'),
            sessions=Count('machine_id'),
            nodes=Sum('dev_nodes'),
            time=Sum('dev_time'),
            latest=Max('updated'),
            **{f'total_{field}': Sum(field) for field in COUNTERS},
        )
        .order_by('-latest', '-test_id')[:limit]
    )
    return [dict(row) for row in rows]


def load_host_detail(
    machine_id: int, now: datetime, limit: int = CONTRIBUTION_LIMIT, session_limit: int = SESSION_LIMIT
) -> HostDetail | None:
    if not (selected := Machine.objects.select_related('user').filter(id=machine_id).first()):
        return None

    registrations = Machine.objects.filter(host_key=selected.host_key)
    current = registrations.select_related('user').order_by('-updated', '-id')[0]
    seen = registrations.aggregate(sessions=Count('id'), first_seen=Min('updated'))
    results = Result.objects.filter(machine__host_key=selected.host_key)
    played = results.aggregate(workloads=Count('test_id', distinct=True), games=Sum('games', default=0))

    sessions = load_sessions(selected.host_key, selected.id, session_limit)
    contributions = load_contributions(selected.host_key, limit)
    workloads = load_workloads(
        [current.workload, *(row['workload'] for row in sessions), *(row['test_id'] for row in contributions)]
    )

    return HostDetail(
        selected=selected,
        current=current,
        host=host_summary(current, seen['first_seen'], played['games'], workloads, now),
        sessions_total=seen['sessions'],
        sessions=[host_session(row, workloads, selected.id, now) for row in sessions],
        workloads_total=played['workloads'],
        contributions=[workload_contribution(workloads[row['test_id']], row, now) for row in contributions],
    )
