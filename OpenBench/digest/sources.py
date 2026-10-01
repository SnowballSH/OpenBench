from datetime import UTC, datetime

from django.db import connection
from django.db.models import Count, DateTimeField, F, Func, Max, Q, QuerySet, Value
from django.db.models.functions import TruncHour

from OpenBench.digest.domain import FINISHED_SENT, DigestWindow, FinishedCounts
from OpenBench.models import Result, Test, WorkloadSnapshot
from OpenBench.page_queries import listing_tests
from OpenBench.progress.sources import FINISH_SLACK, finish_time

type HourMaximum = tuple[int, datetime, int]
type HostRow = tuple[str, str, object, object]

SQLITE_HOUR = '%Y-%m-%d %H:00:00'


def finished_tests(window: DigestWindow) -> QuerySet[Test]:
    recent = Test.objects.filter(finished=True, deleted=False, updated__gte=window.since - FINISH_SLACK)
    ended = recent.annotate(ended=finish_time()).filter(ended__gte=window.since, ended__lte=window.until)
    return ended.order_by('-ended', '-id')


def load_finished_counts(window: DigestWindow) -> FinishedCounts:
    counts = finished_tests(window).aggregate(
        total=Count('id'), passed=Count('id', filter=Q(passed=True)), failed=Count('id', filter=Q(failed=True))
    )
    return FinishedCounts(counts['total'], counts['passed'], counts['failed'])


def load_finished(window: DigestWindow) -> list[Test]:
    return list(listing_tests(finished_tests(window), window.until)[:FINISHED_SENT])


def load_unfinished(window: DigestWindow) -> list[Test]:
    return list(listing_tests(Test.objects.filter(finished=False, deleted=False), window.until).order_by('-id'))


def utc_hour(field: str) -> Func:
    # SQLite stores DateTimeField values as UTC text under USE_TZ, and its
    # built-in strftime() avoids TruncHour's per-row Python function.
    if connection.vendor == 'sqlite':
        return Func(Value(SQLITE_HOUR), F(field), function='strftime', output_field=DateTimeField())
    return TruncHour(field, tzinfo=UTC)


def load_hour_maxima(window: DigestWindow) -> list[HourMaximum]:
    snapshots = WorkloadSnapshot.objects.order_by().filter(created__gte=window.since, created__lte=window.until)
    rows = snapshots.annotate(hour=utc_hour('created')).values('test_id', 'hour').annotate(most=Max('games'))
    return [(row['test_id'], row['hour'], row['most']) for row in rows]


def load_baselines(window: DigestWindow) -> dict[int, int]:
    in_window = WorkloadSnapshot.objects.order_by().filter(created__gte=window.since, created__lte=window.until)
    earlier = WorkloadSnapshot.objects.order_by().filter(
        created__lt=window.since, test_id__in=in_window.values('test_id').distinct()
    )
    return dict(earlier.values('test_id').annotate(most=Max('games')).values_list('test_id', 'most'))


def load_active_hosts(window: DigestWindow) -> list[HostRow]:
    reported = Result.objects.order_by().filter(games__gt=0, updated__gte=window.since, updated__lte=window.until)
    return list(
        reported.values_list(
            'machine__user__username', 'machine__host_key', 'machine__info__machine_name', 'machine__info__cpu_name'
        ).distinct()
    )
