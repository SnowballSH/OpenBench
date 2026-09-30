import logging
from collections.abc import Sequence
from datetime import datetime, timedelta

from django.db import transaction
from django.db.models import Count, Max
from django.utils import timezone

from OpenBench.models import Test, WorkloadSnapshot

SNAPSHOT_INTERVAL = timedelta(seconds=60)
SNAPSHOT_LIMIT = 400
SNAPSHOT_TARGET = 200
RECENT_KEPT = timedelta(hours=1)

logger = logging.getLogger(__name__)


def should_record(
    last: datetime | None, now: datetime, finished: bool, interval: timedelta = SNAPSHOT_INTERVAL
) -> bool:
    return last is None or finished or now - last >= interval


def thinned_ids(
    points: Sequence[tuple[int, datetime]], target: int = SNAPSHOT_TARGET, recent: timedelta = RECENT_KEPT
) -> list[int]:

    # Keep the first point, every point of the last `recent` span, and the
    # newest older point of each of `target` equal-width time buckets.

    ordered = sorted(points, key=lambda point: (point[1], point[0]))
    if len(ordered) <= target + 1:
        return []

    cutoff = ordered[-1][1] - recent
    older = [point for point in ordered if point[1] < cutoff]
    if len(older) <= target + 1:
        return []

    start = older[0][1]
    width = (cutoff - start) / target

    newest: dict[int, int] = {}
    for position, (_, created) in enumerate(older):
        bucket = min(target - 1, int((created - start) / width)) if width else 0
        newest[bucket] = position

    keep = {0, *newest.values()}
    return [older[position][0] for position in range(len(older)) if position not in keep]


def snapshot_of(test: Test, now: datetime) -> WorkloadSnapshot:
    return WorkloadSnapshot(
        test=test,
        created=now,
        games=test.games,
        losses=test.losses,
        draws=test.draws,
        wins=test.wins,
        LL=test.LL,
        LD=test.LD,
        DD=test.DD,
        DW=test.DW,
        WW=test.WW,
        llr=test.currentllr,
    )


def origin_of(test: Test) -> WorkloadSnapshot:
    return WorkloadSnapshot(test=test, created=test.creation)


def thin_history(test_id: int) -> int:
    history = WorkloadSnapshot.objects.filter(test_id=test_id)
    if not (doomed := thinned_ids(list(history.values_list('id', 'created')))):
        return 0
    return WorkloadSnapshot.objects.filter(id__in=doomed).delete()[0]


def record_snapshot(test: Test, reported_games: int, now: datetime | None = None) -> WorkloadSnapshot | None:

    now = now or timezone.now()
    history = WorkloadSnapshot.objects.filter(test_id=test.id).aggregate(last=Max('created'), count=Count('id'))

    if not should_record(history['last'], now, test.finished):
        return None

    if history['last'] is None and test.games > reported_games:
        origin_of(test).save()

    snapshot = snapshot_of(test, now)
    snapshot.save()

    if history['count'] + 1 > SNAPSHOT_LIMIT:
        thin_history(test.id)

    return snapshot


def record_snapshot_safely(test: Test, reported_games: int) -> WorkloadSnapshot | None:

    # History is derived data: a failure here must never roll back a worker's results
    try:
        with transaction.atomic():
            return record_snapshot(test, reported_games)
    except Exception:
        logger.exception('Unable to record a snapshot for Workload %d', test.id)
        return None
