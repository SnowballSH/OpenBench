from collections.abc import Collection, Iterable
from dataclasses import dataclass
from datetime import datetime

from django.db.models import Count, Max, Q, Sum

from OpenBench.fleet.machines import online_since, threads_of
from OpenBench.fleet.status import relative_age
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


def user_row(
    profile: Profile, machines: MachineStats, last_test: datetime | None, now: datetime
) -> UserRow:
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


def load_machine_stats(now: datetime) -> dict[int, MachineStats]:
    online = online_since(now)
    rows = (
        Machine.objects.order_by()
        .values("user_id")
        .annotate(
            online=Count("id", filter=online),
            threads=Sum(threads_of(), filter=online),
            last_heartbeat=Max("updated"),
        )
    )
    return {
        row["user_id"]: MachineStats(
            row["online"], row["threads"] or 0, row["last_heartbeat"]
        )
        for row in rows
    }


def load_listed_profiles(online_owners: Collection[int]) -> list[Profile]:
    listed = (
        Q(games__gt=0)
        | Q(tests__gt=0)
        | Q(approver=True)
        | Q(user_id__in=online_owners)
    )
    return list(
        Profile.objects.select_related("user")
        .filter(listed)
        .order_by("-games", "-tests", "user__username")
    )


def load_latest_tests(usernames: Collection[str]) -> dict[str, datetime]:
    rows = (
        Test.objects.filter(author__in=usernames)
        .order_by()
        .values("author")
        .annotate(latest=Max("creation"))
    )
    return {row["author"]: row["latest"] for row in rows}


def load_user_rows(now: datetime) -> list[UserRow]:
    machines = load_machine_stats(now)
    profiles = load_listed_profiles(
        [user for user, stats in machines.items() if stats.online]
    )
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
