from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import ClassVar, TypedDict

from django.db.models import Count, F, Min, Sum
from django.db.models.fields.json import KT

from OpenBench.fleet.hosts import HostKey
from OpenBench.fleet.housekeeping import rekey_unkeyed
from OpenBench.fleet.pools import Pool, PoolKey, pool_label, short_name
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

    is_pool: ClassVar[bool] = False

    @property
    def online(self) -> bool:
        return self.presence == Presence.ONLINE

    @property
    def total_mnps(self) -> float:
        return self.threads * self.mnps

    @property
    def label(self) -> str | None:
        return short_name(self.name) if self.name else None

    @property
    def pool(self) -> Pool:
        return Pool(self.owner, pool_label(self.name, self.cpu_name), self.cpu_name)

    @property
    def hosts(self) -> int:
        return 1


@dataclass(frozen=True, slots=True)
class PoolRow:
    key: PoolKey
    name: str
    owner: str
    cpu_name: str
    hosts: int
    sessions: int
    lifetime_games: int
    first_seen: datetime
    last_seen: datetime
    last_seen_ago: str

    is_pool: ClassVar[bool] = True
    online: ClassVar[bool] = False


type ListedRow = HostRow | PoolRow


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
    rows: list[ListedRow]
    summary: FleetSummary
    cpus: list[CpuGroup]
    expanded: Pool | None

    @property
    def truncated(self) -> bool:
        return self.summary.online + self.offline_listed < self.summary.hosts

    @property
    def offline_listed(self) -> int:
        return sum(row.hosts for row in self.rows if not row.online)


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


def pool_row(pool: Pool, hosts: Sequence[HostRow], now: datetime) -> PoolRow:
    last_seen = max(host.last_seen for host in hosts)
    return PoolRow(
        key=pool.key,
        name=pool.label,
        owner=pool.owner,
        cpu_name=pool.cpu_name,
        hosts=len(hosts),
        sessions=sum(host.sessions for host in hosts),
        lifetime_games=sum(host.lifetime_games for host in hosts),
        first_seen=min(host.first_seen for host in hosts),
        last_seen=last_seen,
        last_seen_ago=relative_age(now - last_seen),
    )


def roll_up(offline: Iterable[HostRow], expanded: PoolKey | None, now: datetime) -> list[ListedRow]:
    pools: dict[Pool, list[HostRow]] = {}
    for host in offline:
        pools.setdefault(host.pool, []).append(host)

    rows: list[ListedRow] = []
    for pool, hosts in pools.items():
        if len(hosts) == 1 or pool.key == expanded:
            rows.extend(hosts)
        else:
            rows.append(pool_row(pool, hosts, now))
    return rows


def online_order(row: HostRow) -> tuple[str, int]:
    return (row.cpu_name.lower(), row.machine_id)


def offline_order(row: ListedRow) -> tuple[float, str, int]:
    return (-row.last_seen.timestamp(), row.name or '', row.hosts)


def listed(rows: Sequence[HostRow], offline_limit: int, expanded: PoolKey | None, now: datetime) -> list[ListedRow]:
    online = sorted((row for row in rows if row.online), key=online_order)
    offline = sorted(roll_up((row for row in rows if not row.online), expanded, now), key=offline_order)
    return [*online, *offline[:offline_limit]]


def with_workload(row: ListedRow, workloads: dict[int, Test]) -> ListedRow:
    return replace(row, workload=workloads.get(row.workload_id)) if isinstance(row, HostRow) else row


def expanded_pool(rows: Iterable[HostRow], expanded: PoolKey | None) -> Pool | None:
    return next((row.pool for row in rows if not row.online and row.pool.key == expanded), None)


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


def load_keyed_sessions(since: datetime) -> list[CurrentSession]:
    sessions = load_current_sessions(since)
    if all(session['host_key'] for session in sessions):
        return sessions
    rekey_unkeyed()
    return load_current_sessions(since)


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


def load_machines_page(
    now: datetime, window: OfflineWindow, offline_limit: int | None = None, expanded: PoolKey | None = None
) -> MachinesPage:
    limit = OFFLINE_LISTED if offline_limit is None else offline_limit
    since = now - window.span
    sessions = load_keyed_sessions(since)
    totals = load_host_totals(since)

    hosts = [host_row(session, totals.get(session['host_key']), now) for session in sessions]
    shown = listed(hosts, limit, expanded, now)
    workloads = load_workloads(row.workload_id for row in shown if isinstance(row, HostRow))
    cpus = cpu_groups(hosts)

    return MachinesPage(
        window=window,
        windows=tuple(OfflineWindow),
        rows=[with_workload(row, workloads) for row in shown],
        summary=summarize_fleet(cpus, load_games_since(now - GAMES_WINDOW)),
        cpus=cpus,
        expanded=expanded_pool(hosts, expanded),
    )
