from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import TypedDict

from django.db.models import Count, F, Min, Sum
from django.db.models.fields.json import KT

from OpenBench.fleet.hosts import HostKey
from OpenBench.fleet.sessions import current_sessions, threads_of
from OpenBench.fleet.status import (
    UNKNOWN,
    OfflineWindow,
    Presence,
    presence,
    relative_age,
)
from OpenBench.insights.server import GAMES_WINDOW, load_games_since
from OpenBench.machine_info import known_text
from OpenBench.models import Machine, Result, Test

OFFLINE_LISTED = 200


class CurrentSession(TypedDict):
    id: int
    host_key: str
    owner: str
    mnps: float
    updated: datetime
    workload: int
    name: str | None
    cpu: str | None
    isa: str | None
    os: str | None
    threads: int | None


@dataclass(frozen=True, slots=True)
class HostTotals:
    sessions: int
    first_seen: datetime
    lifetime_games: int


@dataclass(frozen=True, slots=True)
class HostRow:
    key: HostKey
    machine_id: int
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
    workload_id: int
    workload: Test | None
    sessions: int
    lifetime_games: int

    @property
    def online(self) -> bool:
        return self.presence == Presence.ONLINE

    @property
    def total_mnps(self) -> float:
        return self.threads * self.mnps


@dataclass(frozen=True, slots=True)
class CpuGroup:
    cpu_name: str
    online: int
    hosts: int
    threads: int
    mnps: float
    lifetime_games: int


@dataclass(frozen=True, slots=True)
class FleetSummary:
    online: int
    offline: int
    threads: int
    mnps: float
    cpu_models: int
    games_last_24h: int

    @property
    def hosts(self) -> int:
        return self.online + self.offline


@dataclass(frozen=True, slots=True)
class MachinesPage:
    window: OfflineWindow
    windows: tuple[OfflineWindow, ...]
    rows: list[HostRow]
    summary: FleetSummary
    cpus: list[CpuGroup]

    @property
    def truncated(self) -> bool:
        return len(self.rows) < self.summary.hosts

    @property
    def offline_listed(self) -> int:
        return sum(not row.online for row in self.rows)


def host_row(session: CurrentSession, totals: HostTotals | None, now: datetime) -> HostRow:
    totals = totals or HostTotals(1, session['updated'], 0)
    return HostRow(
        key=HostKey(session['host_key']),
        machine_id=session['id'],
        name=known_text(session['name']),
        owner=session['owner'],
        cpu_name=known_text(session['cpu']) or UNKNOWN,
        isa_name=known_text(session['isa']),
        os_name=known_text(session['os']),
        threads=session['threads'] or 0,
        mnps=session['mnps'],
        first_seen=totals.first_seen,
        last_seen=session['updated'],
        last_seen_ago=relative_age(now - session['updated']),
        presence=presence(session['updated'], now),
        workload_id=session['workload'],
        workload=None,
        sessions=totals.sessions,
        lifetime_games=totals.lifetime_games,
    )


def display_key(row: HostRow) -> tuple[int, str, float, int]:
    if row.online:
        return (0, row.cpu_name.lower(), 0.0, row.machine_id)
    return (1, '', -row.last_seen.timestamp(), row.machine_id)


def display_order(rows: Iterable[HostRow]) -> list[HostRow]:
    return sorted(rows, key=display_key)


def listed(rows: Iterable[HostRow], offline_limit: int) -> list[HostRow]:
    ordered = display_order(rows)
    online = sum(row.online for row in ordered)
    return ordered[: online + offline_limit]


def cpu_groups(rows: Iterable[HostRow]) -> list[CpuGroup]:
    totals: dict[str, list[float]] = {}

    for row in rows:
        total = totals.setdefault(row.cpu_name, [0, 0, 0, 0.0, 0])
        total[1] += 1
        total[4] += row.lifetime_games
        if row.online:
            total[0] += 1
            total[2] += row.threads
            total[3] += row.total_mnps

    groups = [
        CpuGroup(cpu, int(online), int(hosts), int(threads), float(mnps), int(games))
        for cpu, (online, hosts, threads, mnps, games) in totals.items()
    ]
    return sorted(groups, key=lambda group: (-group.mnps, -group.hosts, group.cpu_name.lower()))


def summarize_fleet(groups: Sequence[CpuGroup], games_last_24h: int) -> FleetSummary:
    online = sum(group.online for group in groups)
    return FleetSummary(
        online=online,
        offline=sum(group.hosts for group in groups) - online,
        threads=sum(group.threads for group in groups),
        mnps=sum(group.mnps for group in groups),
        cpu_models=sum(1 for group in groups if group.online),
        games_last_24h=games_last_24h,
    )


def load_workloads(ids: Iterable[int]) -> dict[int, Test]:
    return Test.objects.select_related('dev').in_bulk([pk for pk in set(ids) if pk])


def load_current_sessions(since: datetime) -> list[CurrentSession]:
    rows = current_sessions(Machine.objects.filter(updated__gte=since)).values(
        'id',
        'host_key',
        'mnps',
        'updated',
        'workload',
        owner=F('user__username'),
        name=KT('info__machine_name'),
        cpu=KT('info__cpu_name'),
        isa=KT('info__isa_name'),
        os=KT('info__os_name'),
        threads=threads_of(),
    )
    return [CurrentSession(**row) for row in rows]


def load_host_totals(since: datetime) -> dict[str, HostTotals]:
    seen = Machine.objects.filter(updated__gte=since).values('host_key')
    sessions = (
        Machine.objects.filter(host_key__in=seen)
        .order_by()
        .values('host_key')
        .annotate(sessions=Count('id'), first_seen=Min('updated'))
    )
    games = dict(
        Result.objects.filter(machine__host_key__in=seen)
        .order_by()
        .values('machine__host_key')
        .annotate(games=Sum('games'))
        .values_list('machine__host_key', 'games')
    )
    return {
        row['host_key']: HostTotals(row['sessions'], row['first_seen'], games.get(row['host_key'], 0))
        for row in sessions
    }


def load_machines_page(now: datetime, window: OfflineWindow, offline_limit: int | None = None) -> MachinesPage:
    limit = OFFLINE_LISTED if offline_limit is None else offline_limit
    since = now - window.span
    sessions = load_current_sessions(since)
    totals = load_host_totals(since)

    hosts = [host_row(session, totals.get(session['host_key']), now) for session in sessions]
    shown = listed(hosts, limit)
    workloads = load_workloads(row.workload_id for row in shown)
    cpus = cpu_groups(hosts)

    return MachinesPage(
        window=window,
        windows=tuple(OfflineWindow),
        rows=[replace(row, workload=workloads.get(row.workload_id)) for row in shown],
        summary=summarize_fleet(cpus, load_games_since(now - GAMES_WINDOW)),
        cpus=cpus,
    )
