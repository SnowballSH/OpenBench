from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from django.db.models import OuterRef, Subquery, Sum
from django.db.models.functions import Coalesce

from OpenBench.fleet.status import UNKNOWN, OfflineWindow, Presence, int_of, presence, relative_age, text_of
from OpenBench.insights.server import GAMES_WINDOW, load_games_since
from OpenBench.models import Machine, Result, Test

@dataclass(frozen=True, slots=True)
class MachineRow:
    id             : int
    name           : str | None
    owner          : str
    cpu_name       : str
    isa_name       : str | None
    os_name        : str | None
    threads        : int
    mnps           : float
    last_seen      : datetime
    last_seen_ago  : str
    presence       : Presence
    workload       : Test | None
    lifetime_games : int

    @property
    def online(self) -> bool:
        return self.presence == Presence.ONLINE

    @property
    def total_mnps(self) -> float:
        return self.threads * self.mnps

@dataclass(frozen=True, slots=True)
class FleetSummary:
    online         : int
    offline        : int
    threads        : int
    mnps           : float
    cpu_models     : int
    games_last_24h : int

@dataclass(frozen=True, slots=True)
class CpuGroup:
    cpu_name       : str
    online         : int
    shown          : int
    threads        : int
    mnps           : float
    lifetime_games : int

@dataclass(frozen=True, slots=True)
class MachinesPage:
    window  : OfflineWindow
    windows : tuple[OfflineWindow, ...]
    rows    : list[MachineRow]
    summary : FleetSummary
    cpus    : list[CpuGroup]

def machine_row(machine: Machine, lifetime_games: int, workloads: Mapping[int, Test], now: datetime) -> MachineRow:
    info = machine.info or {}
    return MachineRow(
        id             = machine.id,
        name           = text_of(info, 'machine_name'),
        owner          = machine.user.username,
        cpu_name       = text_of(info, 'cpu_name') or UNKNOWN,
        isa_name       = text_of(info, 'isa_name'),
        os_name        = text_of(info, 'os_name'),
        threads        = int_of(info, 'concurrency'),
        mnps           = machine.mnps,
        last_seen      = machine.updated,
        last_seen_ago  = relative_age(now - machine.updated),
        presence       = presence(machine.updated, now),
        workload       = workloads.get(machine.workload),
        lifetime_games = lifetime_games,
    )

def display_order(rows: Iterable[MachineRow]) -> list[MachineRow]:
    return sorted(rows, key=lambda row: (not row.online, row.cpu_name.lower(), row.id))

def summarize_fleet(rows: Sequence[MachineRow], games_last_24h: int) -> FleetSummary:
    online = [row for row in rows if row.online]
    return FleetSummary(
        online         = len(online),
        offline        = len(rows) - len(online),
        threads        = sum(row.threads for row in online),
        mnps           = sum(row.total_mnps for row in online),
        cpu_models     = len({ row.cpu_name for row in online }),
        games_last_24h = games_last_24h,
    )

def group_by_cpu(rows: Iterable[MachineRow]) -> list[CpuGroup]:

    groups: dict[str, list[MachineRow]] = {}
    for row in rows:
        groups.setdefault(row.cpu_name, []).append(row)

    summaries = [
        CpuGroup(
            cpu_name       = cpu_name,
            online         = sum(row.online for row in members),
            shown          = len(members),
            threads        = sum(row.threads for row in members if row.online),
            mnps           = sum(row.total_mnps for row in members if row.online),
            lifetime_games = sum(row.lifetime_games for row in members),
        )
        for cpu_name, members in groups.items()
    ]

    return sorted(summaries, key=lambda group: (-group.mnps, -group.shown, group.cpu_name.lower()))

def lifetime_games_of_machine() -> Coalesce:
    per_machine = Result.objects.filter(machine=OuterRef('pk')).values('machine').annotate(total=Sum('games')).values('total')
    return Coalesce(Subquery(per_machine), 0)

def load_workloads(ids: Iterable[int]) -> dict[int, Test]:
    return Test.objects.select_related('dev').in_bulk([pk for pk in set(ids) if pk])

def load_machine_rows(now: datetime, window: OfflineWindow) -> list[MachineRow]:

    machines  = list(
        Machine.objects.filter(updated__gte=now - window.span)
            .select_related('user').annotate(lifetime_games=lifetime_games_of_machine())
    )
    workloads = load_workloads(machine.workload for machine in machines)

    return display_order(machine_row(machine, machine.lifetime_games, workloads, now) for machine in machines)

def load_machines_page(now: datetime, window: OfflineWindow) -> MachinesPage:
    rows = load_machine_rows(now, window)
    return MachinesPage(
        window  = window,
        windows = tuple(OfflineWindow),
        rows    = rows,
        summary = summarize_fleet(rows, load_games_since(now - GAMES_WINDOW)),
        cpus    = group_by_cpu(rows),
    )
