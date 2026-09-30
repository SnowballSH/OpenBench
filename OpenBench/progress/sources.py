from datetime import UTC, date, timedelta

from django.db.models import (
    Case,
    Count,
    DateTimeField,
    F,
    Max,
    OuterRef,
    Q,
    QuerySet,
    Subquery,
    Sum,
    When,
)
from django.db.models.functions import Coalesce, TruncDate, TruncWeek

from OpenBench.insights.sources import (
    PENTANOMIAL_FIELDS,
    TRINOMIAL_FIELDS,
    outcomes_of_row,
)
from OpenBench.models import Result, Test, WorkloadSnapshot
from OpenBench.progress.domain import DayMaximum, GreenRow, OutcomeCounts, Scope

FINISH_SLACK = timedelta(hours=1)


def finish_time() -> Case:
    last_report = (
        WorkloadSnapshot.objects.filter(test=OuterRef("pk"))
        .order_by("-created")
        .values("created")[:1]
    )
    return Case(
        When(
            Q(passed=True) | Q(failed=True),
            then=Coalesce(Subquery(last_report), F("updated")),
        ),
        default=F("updated"),
        output_field=DateTimeField(),
    )


def finished_sprts(scope: Scope) -> QuerySet[Test]:
    tests = Test.objects.filter(finished=True, deleted=False, test_mode="SPRT")
    if scope.engine is not None:
        tests = tests.filter(dev_engine=scope.engine)
    if scope.since is not None:
        tests = tests.filter(updated__gte=scope.since - FINISH_SLACK)
    tests = tests.annotate(finished_at=finish_time())
    if scope.since is not None:
        tests = tests.filter(finished_at__gte=scope.since)
    return tests.order_by()


def load_greens(scope: Scope) -> list[GreenRow]:
    rows = (
        finished_sprts(scope)
        .filter(passed=True)
        .alias(bound_sum=F("elolower") + F("eloupper"))
        .filter(bound_sum__gte=0)
        .values(
            "id",
            "dev__name",
            "finished_at",
            "games",
            "elolower",
            "eloupper",
            "use_tri",
            *TRINOMIAL_FIELDS,
            *PENTANOMIAL_FIELDS,
        )
    )
    return [
        GreenRow(
            id=row["id"],
            name=row["dev__name"],
            finished_at=row["finished_at"],
            games=row["games"],
            elo_bounds=(row["elolower"], row["eloupper"]),
            outcomes=outcomes_of_row(row, not row["use_tri"]),
        )
        for row in rows
    ]


def load_weekly_outcomes(scope: Scope) -> dict[date, OutcomeCounts]:
    rows = (
        finished_sprts(scope)
        .annotate(week=TruncWeek("finished_at", tzinfo=UTC))
        .values("week")
        .annotate(
            passed_tests=Count("id", filter=Q(passed=True)),
            failed_tests=Count("id", filter=Q(failed=True)),
            stopped_tests=Count("id", filter=Q(passed=False, failed=False)),
        )
    )
    return {
        row["week"].date(): OutcomeCounts(
            row["passed_tests"], row["failed_tests"], row["stopped_tests"]
        )
        for row in rows
    }


def scoped_snapshots(scope: Scope) -> QuerySet[WorkloadSnapshot]:
    snapshots = WorkloadSnapshot.objects.order_by()
    if scope.engine is not None:
        snapshots = snapshots.filter(test__dev_engine=scope.engine)
    return snapshots


def load_day_maxima(scope: Scope) -> list[DayMaximum]:
    snapshots = scoped_snapshots(scope)
    if scope.since is not None:
        snapshots = snapshots.filter(created__gte=scope.since)
    rows = (
        snapshots.annotate(day=TruncDate("created", tzinfo=UTC))
        .values("test_id", "day")
        .annotate(games=Max("games"))
    )
    return [DayMaximum(row["test_id"], row["day"], row["games"]) for row in rows]


def load_baselines(scope: Scope) -> dict[int, int]:
    if scope.since is None:
        return {}
    in_window = scoped_snapshots(scope).filter(created__gte=scope.since)
    rows = (
        WorkloadSnapshot.objects.order_by()
        .filter(
            created__lt=scope.since, test_id__in=in_window.values("test_id").distinct()
        )
        .values("test_id")
        .annotate(games=Max("games"))
    )
    return {row["test_id"]: row["games"] for row in rows}


def load_games_by_user(scope: Scope) -> dict[str, int]:
    results = Result.objects.order_by()
    if scope.engine is not None:
        results = results.filter(test__dev_engine=scope.engine)
    if scope.since is not None:
        results = results.filter(updated__gte=scope.since)
    rows = (
        results.values("machine__user__username")
        .annotate(total=Sum("games"))
        .filter(total__gt=0)
    )
    return {row["machine__user__username"]: row["total"] for row in rows}


def load_tests_by_author(scope: Scope) -> dict[str, int]:
    tests = Test.objects.order_by().filter(deleted=False)
    if scope.engine is not None:
        tests = tests.filter(dev_engine=scope.engine)
    if scope.since is not None:
        tests = tests.filter(creation__gte=scope.since)
    rows = tests.values("author").annotate(total=Count("id"))
    return {row["author"]: row["total"] for row in rows}
