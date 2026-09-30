from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from django.db.models import (
    Count,
    F,
    FloatField,
    IntegerField,
    OuterRef,
    Q,
    Subquery,
    Sum,
)
from django.db.models.fields.json import KT, KeyTextTransform
from django.db.models.functions import Cast, Coalesce

from OpenBench.fleet.status import (
    UNKNOWN,
    OfflineWindow,
    Presence,
    int_of,
    known_text,
    presence,
    relative_age,
    text_of,
)
from OpenBench.insights.server import ACTIVE_MACHINE, GAMES_WINDOW, load_games_since
from OpenBench.models import Machine, Result, Test

OFFLINE_LISTED = 200


@dataclass(frozen=True, slots=True)
class MachineRow:
    id: int
    name: str | None
    owner: str
    cpu_name: str
    isa_name: str | None
    os_name: str | None
    threads: int
    mnps: float
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
class CpuGroup:
    cpu_name: str
    online: int
    machines: int
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
    def machines(self) -> int:
        return self.online + self.offline


@dataclass(frozen=True, slots=True)
class MachinesPage:
    window: OfflineWindow
    windows: tuple[OfflineWindow, ...]
    rows: list[MachineRow]
    summary: FleetSummary
    cpus: list[CpuGroup]

    @property
    def truncated(self) -> bool:
        return len(self.rows) < self.summary.machines

    @property
    def offline_listed(self) -> int:
        return sum(not row.online for row in self.rows)


def threads_of(prefix: str = '') -> Cast:
    return Cast(KT(f'{prefix}info__concurrency'), IntegerField())


def online_since(now: datetime, prefix: str = '') -> Q:
    return Q(**{f'{prefix}updated__gte': now - ACTIVE_MACHINE})


def machine_row(machine: Machine, lifetime_games: int, workloads: Mapping[int, Test], now: datetime) -> MachineRow:
    info = machine.info or {}
    return MachineRow(
        id=machine.id,
        name=text_of(info, 'machine_name'),
        owner=machine.user.username,
        cpu_name=text_of(info, 'cpu_name') or UNKNOWN,
        isa_name=text_of(info, 'isa_name'),
        os_name=text_of(info, 'os_name'),
        threads=int_of(info, 'concurrency'),
        mnps=machine.mnps,
        last_seen=machine.updated,
        last_seen_ago=relative_age(now - machine.updated),
        presence=presence(machine.updated, now),
        workload=workloads.get(machine.workload),
        lifetime_games=lifetime_games,
    )


def display_key(row: MachineRow) -> tuple[int, str, float, int]:
    if row.online:
        return (0, row.cpu_name.lower(), 0.0, row.id)
    return (1, '', -row.last_seen.timestamp(), row.id)


def display_order(rows: Iterable[MachineRow]) -> list[MachineRow]:
    return sorted(rows, key=display_key)


def merge_cpu_groups(
    machine_rows: Iterable[Mapping[str, Any]], games_rows: Iterable[Mapping[str, Any]]
) -> list[CpuGroup]:
    totals: dict[str, list[float]] = {}

    for row in machine_rows:
        total = totals.setdefault(known_text(row['cpu']) or UNKNOWN, [0, 0, 0, 0.0, 0])
        for index, key in enumerate(('online', 'machines', 'threads', 'mnps')):
            total[index] += row[key] or 0

    for row in games_rows:
        if total := totals.get(known_text(row['cpu']) or UNKNOWN):
            total[4] += row['games'] or 0

    groups = [
        CpuGroup(cpu, int(online), int(machines), int(threads), float(mnps), int(games))
        for cpu, (online, machines, threads, mnps, games) in totals.items()
    ]
    return sorted(groups, key=lambda group: (-group.mnps, -group.machines, group.cpu_name.lower()))


def summarize_fleet(groups: Sequence[CpuGroup], games_last_24h: int) -> FleetSummary:
    online = sum(group.online for group in groups)
    return FleetSummary(
        online=online,
        offline=sum(group.machines for group in groups) - online,
        threads=sum(group.threads for group in groups),
        mnps=sum(group.mnps for group in groups),
        cpu_models=sum(1 for group in groups if group.online),
        games_last_24h=games_last_24h,
    )


def lifetime_games_of_machine() -> Coalesce:
    per_machine = (
        Result.objects.filter(machine=OuterRef('pk')).values('machine').annotate(total=Sum('games')).values('total')
    )
    return Coalesce(Subquery(per_machine), 0)


def load_workloads(ids: Iterable[int]) -> dict[int, Test]:
    return Test.objects.select_related('dev').in_bulk([pk for pk in set(ids) if pk])


def load_listed_machines(now: datetime, window: OfflineWindow, offline_limit: int) -> list[Machine]:
    listed = Machine.objects.select_related('user').annotate(lifetime_games=lifetime_games_of_machine())
    online = list(listed.filter(online_since(now)))
    if window == OfflineWindow.NONE:
        return online

    offline = listed.filter(updated__gte=now - window.span).exclude(online_since(now))
    return online + list(offline.order_by('-updated', '-id')[:offline_limit])


def load_cpu_groups(now: datetime, window: OfflineWindow) -> list[CpuGroup]:
    online = online_since(now)
    machines = (
        Machine.objects.filter(updated__gte=now - window.span)
        .values(cpu=KT('info__cpu_name'))
        .annotate(
            online=Count('id', filter=online),
            machines=Count('id'),
            threads=Sum(threads_of(), filter=online),
            mnps=Sum(Cast(KT('info__concurrency'), FloatField()) * F('mnps'), filter=online),
        )
    )
    games = (
        Result.objects.filter(machine__updated__gte=now - window.span)
        .values(cpu=KeyTextTransform('cpu_name', 'machine__info'))
        .annotate(games=Sum('games'))
    )
    return merge_cpu_groups(machines, games)


def load_machines_page(now: datetime, window: OfflineWindow, offline_limit: int | None = None) -> MachinesPage:
    limit = OFFLINE_LISTED if offline_limit is None else offline_limit
    machines = load_listed_machines(now, window, limit)
    workloads = load_workloads(machine.workload for machine in machines)
    rows = display_order(machine_row(machine, machine.lifetime_games, workloads, now) for machine in machines)
    cpus = load_cpu_groups(now, window)

    return MachinesPage(
        window=window,
        windows=tuple(OfflineWindow),
        rows=rows,
        summary=summarize_fleet(cpus, load_games_since(now - GAMES_WINDOW)),
        cpus=cpus,
    )
