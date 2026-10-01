from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, TypedDict, TypeIs, cast

from django.db.models import (
    Case,
    Count,
    DateTimeField,
    Field,
    IntegerField,
    OuterRef,
    QuerySet,
    Subquery,
    Value,
    When,
)
from django.db.models.expressions import BaseExpression
from django.db.models.functions import Coalesce
from django.http import HttpRequest
from django.utils import timezone

import OpenBench.config
import OpenBench.utils
from OpenBench.diagnosis.listing import attach_row_reasons, attach_stop_reasons
from OpenBench.insights.listing import RowTiming, SnapshotMarks, finished_row_timing, running_row_timing
from OpenBench.insights.sources import workload_facts
from OpenBench.insights.timing import RECENT_WINDOW, Mark
from OpenBench.models import Network, Profile, SPSAParameter, Test, WorkloadSnapshot
from OpenBench.progress.sources import finish_time

if TYPE_CHECKING:
    from django_stubs_ext import WithAnnotations

PROFILE_ATTRIBUTE = '_openbench_profile'


class ListingTimes(TypedDict):
    listing_as_of: datetime
    finished_at: datetime
    first_snapshot_at: datetime | None


type ListedTest = WithAnnotations[Test, ListingTimes]


def request_profile(request: HttpRequest) -> Profile | None:

    if not request.user.is_authenticated:
        return None

    if not hasattr(request, PROFILE_ATTRIBUTE):
        profile = Profile.objects.select_related('user').filter(user=request.user).first()
        setattr(request, PROFILE_ATTRIBUTE, profile)

    cached: Profile | None = getattr(request, PROFILE_ATTRIBUTE)
    return cached


def may_poll(profile: object) -> bool:
    # Mirrors api_authenticate, so a page never loads a poller the API would refuse
    # Read directly: OpenBench.upstream imports the listing rows, which import this module
    config = cast(dict[str, Any], OpenBench.config.OPENBENCH_CONFIG)
    if not config['require_login_to_view']:
        return True
    return isinstance(profile, Profile) and profile.enabled


def dev_network_label() -> Subquery:
    networks = Network.objects.filter(engine=OuterRef('dev_engine'), sha256=OuterRef('dev_network'))
    return Subquery(networks.order_by('id').values('name')[:1])


def spsa_parameter_count() -> Coalesce:
    parameters = SPSAParameter.objects.filter(spsa_run__tune=OuterRef('pk')).order_by().values('spsa_run')
    return Coalesce(
        Subquery(parameters.annotate(count=Count('id')).values('count')),
        0,
        output_field=IntegerField(),
    )


def snapshot_pick(
    name: str, snapshots: QuerySet[WorkloadSnapshot], *, newest: bool, running_only: bool
) -> dict[str, BaseExpression]:

    ordered = snapshots.order_by('-created', '-id') if newest else snapshots.order_by('created', 'id')

    def column(field: str, output: Field[Any, Any]) -> BaseExpression:
        value = Subquery(ordered.values(field)[:1], output_field=output)
        return Case(When(finished=False, then=value), output_field=output) if running_only else value

    return {f'{name}_at': column('created', DateTimeField()), f'{name}_games': column('games', IntegerField())}


def listing_timing(now: datetime) -> dict[str, BaseExpression]:

    snapshots = WorkloadSnapshot.objects.filter(test=OuterRef('pk'))
    window_start = now - RECENT_WINDOW
    return {
        'listing_as_of': Value(now, output_field=DateTimeField()),
        'finished_at': finish_time(),
        **snapshot_pick('first_snapshot', snapshots, newest=False, running_only=False),
        **snapshot_pick('window_before', snapshots.filter(created__lte=window_start), newest=True, running_only=True),
        **snapshot_pick('window_after', snapshots.filter(created__gt=window_start), newest=False, running_only=True),
        **snapshot_pick('latest_snapshot', snapshots, newest=True, running_only=True),
    }


def listing_tests(tests: QuerySet[Test], now: datetime | None = None) -> QuerySet[Test]:

    # Everything Blocks/testsummary.html reads, so a listed row costs no further query
    return tests.select_related('dev', 'base', 'spsa_run').annotate(
        dev_network_label=dev_network_label(),
        spsa_parameter_count=spsa_parameter_count(),
        **listing_timing(now or timezone.now()),
    )


def annotated_mark(test: Test, name: str) -> Mark | None:
    at, games = getattr(test, f'{name}_at'), getattr(test, f'{name}_games')
    return None if at is None or games is None else (at, games)


def annotated_snapshots(test: Test) -> SnapshotMarks:
    return SnapshotMarks(
        first=annotated_mark(test, 'first_snapshot'),
        window_before=annotated_mark(test, 'window_before'),
        window_after=annotated_mark(test, 'window_after'),
        latest=annotated_mark(test, 'latest_snapshot'),
    )


def is_listed(test: Test) -> TypeIs[ListedTest]:
    return getattr(test, 'listing_as_of', None) is not None


def listing_row_timing(test: Test) -> RowTiming | None:

    if not is_listed(test):
        return None

    if test.finished:
        return finished_row_timing(test.first_snapshot_at or test.creation, test.finished_at)

    if not test.approved or test.deleted:
        return None

    return running_row_timing(workload_facts(test), annotated_snapshots(test), test.listing_as_of)


def lacks_rate(test: Test) -> bool:
    timing = listing_row_timing(test)
    return timing is None or timing.rate is None


@dataclass(frozen=True)
class FrontRows:
    pending: list[Test]
    active: list[Test]
    status: str


@dataclass(frozen=True)
class FrontPage:
    pending: QuerySet[Test]
    active: QuerySet[Test]
    status: Callable[[], str]

    def shown(self, now: datetime) -> FrontRows:
        active = list(listing_tests(self.active, now))
        attach_row_reasons([test for test in active if lacks_rate(test)], now)
        return FrontRows(pending=list(listing_tests(self.pending, now)), active=active, status=self.status())

    def data(self) -> dict[str, Any]:
        rows = self.shown(timezone.now())
        return {
            'pending': rows.pending,
            'active': OpenBench.utils.group_active_tests_by_priority(rows.active),
            'status': rows.status,
        }


def by_author(tests: QuerySet[Test], author: str | None) -> QuerySet[Test]:
    return tests if author is None else tests.filter(author=author)


def front_page(author: str | None = None) -> FrontPage:
    return FrontPage(
        by_author(OpenBench.utils.get_pending_tests(), author),
        by_author(OpenBench.utils.get_active_tests(), author),
        lambda: OpenBench.utils.getMachineStatus(author),
    )


def unfinished_tests(author: str | None = None) -> QuerySet[Test]:
    return by_author(Test.objects.filter(finished=False, deleted=False), author)


def workload_list_data(
    completed: QuerySet[Test], page: int, url: str, front: FrontPage | None = None
) -> dict[str, Any]:

    # Pending and Active tests, and the Machine status, only show on the first page
    start, end, paging = OpenBench.utils.getPaging(completed, page, url)
    shown = front.data() if front and paging['page'] == 1 else {}
    now = timezone.now()
    listed = list(listing_tests(completed, now)[start:end])
    attach_stop_reasons(listed, now)
    return {**shown, 'completed': listed, 'paging': paging}
