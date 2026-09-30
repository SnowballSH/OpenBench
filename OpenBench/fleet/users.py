from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from django.db.models import Count, IntegerField, Max, OuterRef, Q, QuerySet, Subquery, Sum
from django.db.models.fields.json import KT
from django.db.models.functions import Cast, Coalesce

from OpenBench.fleet.status import relative_age
from OpenBench.insights.server import ACTIVE_MACHINE
from OpenBench.models import Machine, Profile, Test

@dataclass(frozen=True, slots=True)
class UserRow:
    username          : str
    games             : int
    tests             : int
    engine            : str
    joined            : datetime
    machines_online   : int
    threads_online    : int
    last_activity     : datetime | None
    last_activity_ago : str | None

def latest(moments: Iterable[datetime | None]) -> datetime | None:
    return max((moment for moment in moments if moment is not None), default=None)

def machine_aggregate(expression: Count | Sum | Max, **filters: object) -> Subquery:
    owned = Machine.objects.filter(user=OuterRef('user'), **filters).order_by().values('user')
    return Subquery(owned.annotate(value=expression).values('value'))

def annotated_profiles(now: datetime) -> QuerySet[Profile]:

    online  = { 'updated__gte' : now - ACTIVE_MACHINE }
    threads = Sum(Cast(KT('info__concurrency'), IntegerField()))
    newest_test = Test.objects.filter(author=OuterRef('user__username')).order_by('-creation').values('creation')[:1]

    return (
        Profile.objects.select_related('user')
            .annotate(
                machines_online = Coalesce(machine_aggregate(Count('id'), **online), 0),
                threads_online  = Coalesce(machine_aggregate(threads, **online), 0),
                last_heartbeat  = machine_aggregate(Max('updated')),
                last_test       = Subquery(newest_test),
            )
            .filter(Q(games__gt=0) | Q(tests__gt=0) | Q(approver=True) | Q(machines_online__gt=0))
            .order_by('-games', '-tests', 'user__username')
    )

def user_row(profile: Profile, now: datetime) -> UserRow:
    last_activity = latest((profile.user.last_login, profile.last_heartbeat, profile.last_test))
    return UserRow(
        username          = profile.user.username,
        games             = profile.games,
        tests             = profile.tests,
        engine            = profile.engine,
        joined            = profile.user.date_joined,
        machines_online   = profile.machines_online,
        threads_online    = profile.threads_online,
        last_activity     = last_activity,
        last_activity_ago = relative_age(now - last_activity) if last_activity else None,
    )

def load_user_rows(now: datetime) -> list[UserRow]:
    return [user_row(profile, now) for profile in annotated_profiles(now)]
