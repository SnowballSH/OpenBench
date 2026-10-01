from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, timedelta
from typing import Any

from django.db import connection
from django.db.models import (
    Count,
    DateField,
    DateTimeField,
    F,
    Func,
    Max,
    OuterRef,
    Q,
    QuerySet,
    Subquery,
    Sum,
)
from django.db.models.functions import Coalesce, TruncDate, TruncWeek

from OpenBench.insights.sources import (
    PENTANOMIAL_FIELDS,
    TRINOMIAL_FIELDS,
    outcomes_of_row,
)
from OpenBench.models import Result, Test, WorkloadSnapshot
from OpenBench.progress.domain import (
    Commit,
    DayMaximum,
    HostCounters,
    OutcomeCounts,
    RunMode,
    RunRow,
    RunStatus,
    Scope,
    TimeClass,
)
from OpenBench.progress.options import thread_count

FINISH_SLACK = timedelta(hours=1)

COUNTED = Q(dev_nodes__gt=0, dev_time__gt=0, base_nodes__gt=0, base_time__gt=0)

type Classifier = Callable[[str, str, str, str], TimeClass]
type Usage = Mapping[int, Sequence[HostCounters]]


def finish_time() -> Coalesce:
    last_report = WorkloadSnapshot.objects.filter(test=OuterRef('pk')).order_by('-created').values('created')[:1]
    return Coalesce(Subquery(last_report), F('updated'), output_field=DateTimeField())


def start_time() -> Coalesce:
    first_report = WorkloadSnapshot.objects.filter(test=OuterRef('pk')).order_by('created').values('created')[:1]
    return Coalesce(Subquery(first_report), F('creation'), output_field=DateTimeField())


def finished_sprts(scope: Scope) -> QuerySet[Test]:
    tests = Test.objects.filter(finished=True, deleted=False, test_mode='SPRT')
    if scope.engine is not None:
        tests = tests.filter(dev_engine=scope.engine)
    if scope.since is not None:
        tests = tests.filter(updated__gte=scope.since - FINISH_SLACK)
    tests = tests.annotate(finished_at=finish_time())
    if scope.since is not None:
        tests = tests.filter(finished_at__gte=scope.since)
    return tests.order_by()


def run_status(row: dict[str, Any], mode: RunMode) -> RunStatus:
    decisive = mode == RunMode.SPRT
    flags = (
        (decisive and row['passed'], RunStatus.PASSED),
        (decisive and row['failed'], RunStatus.FAILED),
        (row['finished'], RunStatus.STOPPED if decisive else RunStatus.COMPLETED),
        (not row['approved'], RunStatus.PENDING),
    )
    return next((status for flag, status in flags if flag), RunStatus.RUNNING)


def run_row(row: dict[str, Any], classify: Classifier, usage: Usage) -> RunRow:
    mode = RunMode(row['test_mode'])
    return RunRow(
        id=row['id'],
        engine=row['dev_engine'],
        repo=row['dev_repo'],
        base=Commit(row['base__sha'], row['base_network']),
        dev=Commit(row['dev__sha'], row['dev_network']),
        subject=row['info'],
        author=row['author'],
        mode=mode,
        status=run_status(row, mode),
        time_class=classify(row['dev_time_control'], row['base_time_control'], row['dev_options'], row['base_options']),
        time_control=row['dev_time_control'],
        created_at=row['creation'],
        finished_at=row['finished_at'] if row['finished'] else None,
        games=row['games'],
        outcomes=outcomes_of_row(row, not row['use_tri']),
        threads=thread_count(row['dev_options']) or 1,
        started_at=row['started_at'],
        dev_bench=row['dev__bench'],
        base_bench=row['base__bench'],
        hosts=tuple(usage.get(row['id'], ())),
    )


def load_usage(engine: str | None) -> dict[int, list[HostCounters]]:
    results = Result.objects.order_by().filter(
        games__gt=0,
        test__deleted=False,
        test__test_mode__in=list(RunMode),
        test__dev_engine=F('test__base_engine'),
    )
    if engine is not None:
        results = results.filter(test__dev_engine=engine)
    rows = results.values('test_id', 'machine__host_key').annotate(
        played=Sum('games'),
        counted_games=Coalesce(Sum('games', filter=COUNTED), 0),
        total_dev_nodes=Coalesce(Sum('dev_nodes', filter=COUNTED), 0),
        total_dev_time=Coalesce(Sum('dev_time', filter=COUNTED), 0),
        total_base_nodes=Coalesce(Sum('base_nodes', filter=COUNTED), 0),
        total_base_time=Coalesce(Sum('base_time', filter=COUNTED), 0),
    )
    usage: defaultdict[int, list[HostCounters]] = defaultdict(list)
    for row in rows:
        usage[row['test_id']].append(
            HostCounters(
                host=row['machine__host_key'],
                games=row['played'],
                counted_games=row['counted_games'],
                dev_nodes=row['total_dev_nodes'],
                dev_ms=row['total_dev_time'],
                base_nodes=row['total_base_nodes'],
                base_ms=row['total_base_time'],
            )
        )
    return dict(usage)


def load_runs(engine: str | None, classify: Classifier, usage: Usage | None = None) -> list[RunRow]:
    tests = Test.objects.filter(deleted=False, test_mode__in=list(RunMode), dev_engine=F('base_engine'))
    if engine is not None:
        tests = tests.filter(dev_engine=engine)
    rows = (
        tests.annotate(finished_at=finish_time(), started_at=start_time())
        .order_by('id')
        .values(
            'id',
            'dev_engine',
            'dev_repo',
            'dev__sha',
            'base__sha',
            'dev__bench',
            'base__bench',
            'dev_network',
            'base_network',
            'info',
            'author',
            'test_mode',
            'passed',
            'failed',
            'finished',
            'approved',
            'dev_time_control',
            'base_time_control',
            'dev_options',
            'base_options',
            'creation',
            'finished_at',
            'started_at',
            'games',
            'use_tri',
            *TRINOMIAL_FIELDS,
            *PENTANOMIAL_FIELDS,
        )
    )
    return [run_row(row, classify, usage or {}) for row in rows]


def load_weekly_outcomes(scope: Scope) -> dict[date, OutcomeCounts]:
    rows = (
        finished_sprts(scope)
        .annotate(week=TruncWeek('finished_at', tzinfo=UTC))
        .values('week')
        .annotate(
            passed_tests=Count('id', filter=Q(passed=True)),
            failed_tests=Count('id', filter=Q(failed=True)),
            stopped_tests=Count('id', filter=Q(passed=False, failed=False)),
        )
    )
    return {
        row['week'].date(): OutcomeCounts(row['passed_tests'], row['failed_tests'], row['stopped_tests'])
        for row in rows
    }


def scoped_snapshots(scope: Scope) -> QuerySet[WorkloadSnapshot]:
    snapshots = WorkloadSnapshot.objects.order_by()
    if scope.engine is not None:
        snapshots = snapshots.filter(test__dev_engine=scope.engine)
    return snapshots


def utc_date(field: str) -> Func:
    # SQLite stores DateTimeField values as UTC text under USE_TZ, and its
    # built-in date() avoids TruncDate's per-row Python function.
    if connection.vendor == 'sqlite':
        return Func(F(field), function='date', output_field=DateField())
    return TruncDate(field, tzinfo=UTC)


def load_day_maxima(scope: Scope) -> list[DayMaximum]:
    snapshots = scoped_snapshots(scope)
    if scope.since is not None:
        snapshots = snapshots.filter(created__gte=scope.since)
    rows = snapshots.annotate(day=utc_date('created')).values('test_id', 'day').annotate(games=Max('games'))
    return [DayMaximum(row['test_id'], row['day'], row['games']) for row in rows]


def load_baselines(scope: Scope) -> dict[int, int]:
    if scope.since is None:
        return {}
    in_window = scoped_snapshots(scope).filter(created__gte=scope.since)
    rows = (
        WorkloadSnapshot.objects.order_by()
        .filter(created__lt=scope.since, test_id__in=in_window.values('test_id').distinct())
        .values('test_id')
        .annotate(games=Max('games'))
    )
    return {row['test_id']: row['games'] for row in rows}


def load_games_by_user(scope: Scope) -> dict[str, int]:
    results = Result.objects.order_by()
    if scope.engine is not None:
        results = results.filter(test__dev_engine=scope.engine)
    if scope.since is not None:
        results = results.filter(updated__gte=scope.since)
    rows = results.values('machine__user__username').annotate(total=Sum('games')).filter(total__gt=0)
    return {row['machine__user__username']: row['total'] for row in rows}


def load_tests_by_author(scope: Scope) -> dict[str, int]:
    tests = Test.objects.order_by().filter(deleted=False)
    if scope.engine is not None:
        tests = tests.filter(dev_engine=scope.engine)
    if scope.since is not None:
        tests = tests.filter(creation__gte=scope.since)
    rows = tests.values('author').annotate(total=Count('id'))
    return {row['author']: row['total'] for row in rows}
