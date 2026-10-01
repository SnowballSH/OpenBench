import hashlib
from datetime import datetime

from django.db.models import Count, Max, Q, QuerySet, Sum

from OpenBench.live.domain import FRESHNESS
from OpenBench.models import Test

TOKEN_LENGTH = 16
MAX_TOKEN_LENGTH = 64


def freshness_window(now: datetime) -> int:
    return int(now.timestamp() // FRESHNESS.total_seconds())


def change_token(*parts: object) -> str:
    return hashlib.sha256(repr(parts).encode()).hexdigest()[:TOKEN_LENGTH]


def listing_token(unfinished: QuerySet[Test], now: datetime) -> str:
    totals = unfinished.order_by().aggregate(
        count=Count('id'),
        approved=Count('id', filter=Q(approved=True)),
        updated=Max('updated'),
        games=Sum('games'),
    )
    return change_token(totals['count'], totals['approved'], totals['updated'], totals['games'], freshness_window(now))


def workload_token(workload: Test, now: datetime) -> str:
    state = (workload.approved, workload.finished, workload.passed, workload.failed, workload.deleted)
    return change_token(workload.id, workload.updated, workload.games, state, freshness_window(now))
