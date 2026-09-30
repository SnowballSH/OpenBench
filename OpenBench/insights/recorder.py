from collections.abc import Sequence
from datetime import datetime, timedelta

from django.db.models import Count, Max
from django.utils import timezone

from OpenBench.models import Test, WorkloadSnapshot

SNAPSHOT_INTERVAL = timedelta(seconds=60)
SNAPSHOT_LIMIT    = 400
SNAPSHOT_TARGET   = 200

def should_record(last: datetime | None, now: datetime, finished: bool, interval: timedelta = SNAPSHOT_INTERVAL) -> bool:
    return last is None or finished or now - last >= interval

def thinned_ids(points: Sequence[tuple[int, datetime]], target: int = SNAPSHOT_TARGET) -> list[int]:

    # Keep the first point, the last point, and the newest point of each of
    # `target` equal-width time buckets; everything else is deleted.

    if len(points) <= target + 1:
        return []

    ordered      = sorted(points, key=lambda point: (point[1], point[0]))
    start, end   = ordered[0][1], ordered[-1][1]
    width        = (end - start) / target

    newest: dict[int, int] = {}
    for position, (_, created) in enumerate(ordered):
        bucket = min(target - 1, int((created - start) / width)) if width else 0
        newest[bucket] = position

    keep = { 0, len(ordered) - 1, *newest.values() }
    return [ordered[position][0] for position in range(len(ordered)) if position not in keep]

def snapshot_of(test: Test, now: datetime) -> WorkloadSnapshot:
    return WorkloadSnapshot(
        test=test, created=now, games=test.games,
        losses=test.losses, draws=test.draws, wins=test.wins,
        LL=test.LL, LD=test.LD, DD=test.DD, DW=test.DW, WW=test.WW,
        llr=test.currentllr,
    )

def thin_history(test_id: int) -> int:
    history = WorkloadSnapshot.objects.filter(test_id=test_id)
    if not (doomed := thinned_ids(list(history.values_list('id', 'created')))):
        return 0
    return WorkloadSnapshot.objects.filter(id__in=doomed).delete()[0]

def record_snapshot(test: Test, now: datetime | None = None) -> WorkloadSnapshot | None:

    now     = now or timezone.now()
    history = WorkloadSnapshot.objects.filter(test_id=test.id).aggregate(last=Max('created'), count=Count('id'))

    if not should_record(history['last'], now, test.finished):
        return None

    snapshot = snapshot_of(test, now)
    snapshot.save()

    if history['count'] + 1 > SNAPSHOT_LIMIT:
        thin_history(test.id)

    return snapshot
