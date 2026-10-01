from datetime import datetime, timedelta

from django.db.models import Exists, IntegerField, OuterRef, Q, QuerySet
from django.db.models.fields.json import KT
from django.db.models.functions import Cast

from OpenBench.models import Machine, Result

ACTIVE_MACHINE = timedelta(minutes=2)


def threads_of(prefix: str = '') -> Cast:
    return Cast(KT(f'{prefix}info__concurrency'), IntegerField())


def online_since(now: datetime, prefix: str = '') -> Q:
    return Q(**{f'{prefix}updated__gte': now - ACTIVE_MACHINE})


def superseded() -> Q:
    same_host = Machine.objects.filter(host_key=OuterRef('host_key'))
    later = same_host.filter(updated__gt=OuterRef('updated'))
    tied = same_host.filter(updated=OuterRef('updated'), id__gt=OuterRef('id'))
    # An unkeyed row (written by a rolled-back image) is its own host until it is keyed
    return Q(host_key__gt='') & (Q(Exists(later)) | Q(Exists(tied)))


def current_sessions(machines: QuerySet[Machine]) -> QuerySet[Machine]:
    return machines.exclude(superseded())


def never_used(machines: QuerySet[Machine]) -> QuerySet[Machine]:
    return machines.exclude(Exists(Result.objects.filter(machine=OuterRef('pk'))))
