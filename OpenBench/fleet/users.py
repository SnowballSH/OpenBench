from collections.abc import Collection, Iterable
from dataclasses import dataclass
from datetime import datetime

from django.db.models import Max, Q

from OpenBench.fleet.sessions import current_sessions, online_since, threads_of
from OpenBench.fleet.status import relative_age
from OpenBench.insights.grouping import sum_by_key
from OpenBench.models import Machine, Profile, Test


@dataclass(frozen=True, slots=True)
class MachineStats:
    online: int
    threads: int
    last_heartbeat: datetime | None


NO_MACHINES = MachineStats(online=0, threads=0, last_heartbeat=None)


@dataclass(frozen=True, slots=True)
class UserRow:
    username: str
    games: int
    tests: int
    engine: str
    joined: datetime
    machines_online: int
    threads_online: int
    last_activity: datetime | None
    last_activity_ago: str | None


def latest(moments: Iterable[datetime | None]) -> datetime | None:
    return max((moment for moment in moments if moment is not None), default=None)


def user_row(profile: Profile, machines: MachineStats, last_test: datetime | None, now: datetime) -> UserRow:
    last_activity = latest((machines.last_heartbeat, last_test))
    return UserRow(
        username=profile.user.username,
        games=profile.games,
        tests=profile.tests,
        engine=profile.engine,
        joined=profile.user.date_joined,
        machines_online=machines.online,
        threads_online=machines.threads,
        last_activity=last_activity,
        last_activity_ago=relative_age(now - last_activity) if last_activity else None,
    )


def load_online_hosts(now: datetime) -> dict[int, tuple[int, int]]:
    # Summed here, not with GROUP BY, so the query stays on the heartbeat index
    sessions = current_sessions(Machine.objects.filter(online_since(now))).values_list('user_id', threads_of())
    totals = sum_by_key(sessions, lambda row: row[0], lambda row: (1, row[1] or 0), 0)
    return {user_id: (online, threads) for user_id, (online, threads) in totals.items()}


def load_machine_stats(now: datetime) -> dict[int, MachineStats]:
    online = load_online_hosts(now)
    heartbeats = Machine.objects.order_by().values('user_id').annotate(last_heartbeat=Max('updated'))
    return {
        row['user_id']: MachineStats(*online.get(row['user_id'], (0, 0)), row['last_heartbeat']) for row in heartbeats
    }


def load_listed_profiles(online_owners: Collection[int]) -> list[Profile]:
    listed = Q(games__gt=0) | Q(tests__gt=0) | Q(approver=True) | Q(user_id__in=online_owners)
    return list(Profile.objects.select_related('user').filter(listed).order_by('-games', '-tests', 'user__username'))


def load_latest_tests(usernames: Collection[str]) -> dict[str, datetime]:
    rows = Test.objects.filter(author__in=usernames).order_by().values('author').annotate(latest=Max('creation'))
    return {row['author']: row['latest'] for row in rows}


def load_user_rows(now: datetime) -> list[UserRow]:
    machines = load_machine_stats(now)
    profiles = load_listed_profiles([user for user, stats in machines.items() if stats.online])
    tests = load_latest_tests([profile.user.username for profile in profiles])

    return [
        user_row(
            profile,
            machines.get(profile.user_id, NO_MACHINES),
            tests.get(profile.user.username),
            now,
        )
        for profile in profiles
    ]
