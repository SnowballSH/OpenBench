import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import batched

from django.db import DatabaseError, transaction
from django.db.models import Exists, OuterRef, Q, QuerySet
from django.db.models.fields.json import KT

from OpenBench.fleet.hosts import host_key
from OpenBench.fleet.sessions import never_used
from OpenBench.models import Machine, Result

EXIT_WHEN_IDLE_FLAGS = frozenset({'--single_workload', '--fleet'})
EXITED_AFTER = timedelta(minutes=15)
ABANDONED_AFTER = timedelta(days=7)
REGISTRATION_PRUNE_WINDOW = 500
DELETE_BATCH = 500
REKEY_BATCH = 200

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PrunePlan:
    registrations: int
    in_use: int
    unkeyed: int
    exited: list[int]
    abandoned: list[int]

    @property
    def prunable(self) -> list[int]:
        return [*self.exited, *self.abandoned]


def exits_when_idle(cli_options: object) -> bool:
    return isinstance(cli_options, str) and not EXIT_WHEN_IDLE_FLAGS.isdisjoint(cli_options.split())


def idle_candidates(machines: QuerySet[Machine], before: datetime) -> QuerySet[Machine]:
    return never_used(machines.filter(updated__lt=before)).order_by('id')


def exited_sessions(machines: QuerySet[Machine], now: datetime, limit: int | None = None) -> list[int]:
    mentions_flag = Q()
    for flag in EXIT_WHEN_IDLE_FLAGS:
        mentions_flag |= Q(cli_options__contains=flag)
    rows = (
        idle_candidates(machines, now - EXITED_AFTER)
        .annotate(cli_options=KT('info__cli_options'))
        .filter(mentions_flag)
        .values_list('id', 'cli_options')
    )
    return [machine_id for machine_id, cli_options in rows[:limit] if exits_when_idle(cli_options)]


def delete_registrations(ids: Iterable[int]) -> int:
    # Result.machine is PROTECT, and the filter is re-applied here: a registration that gained a Result is kept
    unused = never_used(Machine.objects.filter(id__in=list(ids)))
    return unused.only('id').delete()[1].get(Machine._meta.label, 0)


def stale_exited_sessions(owner_id: int, now: datetime, window: int) -> list[int]:
    # Walks the owner's newest stale registrations down the (user, updated) index and stops at the window
    stale = Machine.objects.filter(user_id=owner_id, updated__lt=now - EXITED_AFTER).order_by('-updated')
    rows = stale.annotate(used=Exists(Result.objects.filter(machine=OuterRef('pk')))).values_list(
        'id', 'used', KT('info__cli_options')
    )
    return [machine_id for machine_id, used, cli_options in rows[:window] if not used and exits_when_idle(cli_options)]


def prune_exited_sessions(owner_id: int, now: datetime, window: int = REGISTRATION_PRUNE_WINDOW) -> int:
    stale = stale_exited_sessions(owner_id, now, window)
    return delete_registrations(stale) if stale else 0


def unkeyed() -> QuerySet[Machine]:
    return Machine.objects.filter(host_key='')


def rekey_unkeyed(limit: int = REKEY_BATCH) -> int:
    rows = unkeyed().order_by('-id').values_list('id', 'user__username', 'info')[:limit]
    keyed = [Machine(id=machine_id, host_key=host_key(owner, info)) for machine_id, owner, info in rows]
    return Machine.objects.bulk_update(keyed, ['host_key']) if keyed else 0


def rekey_all_unkeyed() -> int:
    total = 0
    while keyed := rekey_unkeyed():
        total += keyed
    return total


def registration_housekeeping(owner_id: int, now: datetime) -> None:
    # Best effort, in its own transaction: a registration never fails because tidying up did
    try:
        with transaction.atomic():
            rekey_unkeyed()
            prune_exited_sessions(owner_id, now)
    except DatabaseError:
        logger.exception('Machine housekeeping failed; the registration is unaffected')


def plan_prune(now: datetime, abandoned_after: timedelta | None = None) -> PrunePlan:
    exited = exited_sessions(Machine.objects.all(), now)
    registrations = Machine.objects.count()
    return PrunePlan(
        registrations=registrations,
        in_use=registrations - never_used(Machine.objects.all()).count(),
        unkeyed=unkeyed().count(),
        exited=exited,
        abandoned=abandoned_sessions(now, abandoned_after, exited) if abandoned_after else [],
    )


def abandoned_sessions(now: datetime, after: timedelta, exited: Iterable[int]) -> list[int]:
    idle = idle_candidates(Machine.objects.all(), now - max(after, EXITED_AFTER))
    return sorted(set(idle.values_list('id', flat=True)) - set(exited))


def apply_prune(plan: PrunePlan) -> int:
    return sum(delete_registrations(batch) for batch in batched(plan.prunable, DELETE_BATCH, strict=False))
