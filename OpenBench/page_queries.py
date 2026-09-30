from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from django.db.models import Count, IntegerField, OuterRef, QuerySet, Subquery
from django.db.models.functions import Coalesce
from django.http import HttpRequest

import OpenBench.utils
from OpenBench.models import LogEvent, Network, Profile, SPSAParameter, Test

PROFILE_ATTRIBUTE = "_openbench_profile"


def request_profile(request: HttpRequest) -> Profile | None:

    if not request.user.is_authenticated:
        return None

    if not hasattr(request, PROFILE_ATTRIBUTE):
        profile = (
            Profile.objects.select_related("user").filter(user=request.user).first()
        )
        setattr(request, PROFILE_ATTRIBUTE, profile)

    return getattr(request, PROFILE_ATTRIBUTE)


def dev_network_label() -> Subquery:
    networks = Network.objects.filter(
        engine=OuterRef("dev_engine"), sha256=OuterRef("dev_network")
    )
    return Subquery(networks.order_by("id").values("name")[:1])


def spsa_parameter_count() -> Coalesce:
    parameters = (
        SPSAParameter.objects.filter(spsa_run__tune=OuterRef("pk"))
        .order_by()
        .values("spsa_run")
    )
    return Coalesce(
        Subquery(parameters.annotate(count=Count("id")).values("count")),
        0,
        output_field=IntegerField(),
    )


def listing_tests(tests: QuerySet[Test]) -> QuerySet[Test]:

    # Everything Blocks/testsummary.html reads, so a listed row costs no further query
    return tests.select_related("dev", "base", "spsa_run").annotate(
        dev_network_label=dev_network_label(),
        spsa_parameter_count=spsa_parameter_count(),
    )


@dataclass(frozen=True)
class FrontPage:
    pending: QuerySet[Test]
    active: QuerySet[Test]
    status: Callable[[], str]

    def data(self) -> dict[str, Any]:
        active = listing_tests(self.active)
        return {
            "pending": listing_tests(self.pending),
            "active": OpenBench.utils.group_active_tests_by_priority(active),
            "status": self.status(),
        }


def workload_list_data(
    completed: QuerySet[Test], page: int, url: str, front: FrontPage | None = None
) -> dict[str, Any]:

    # Pending and Active tests, and the Machine status, only show on the first page
    start, end, paging = OpenBench.utils.getPaging(completed, page, url)
    shown = front.data() if front and paging["page"] == 1 else {}
    return {**shown, "completed": listing_tests(completed)[start:end], "paging": paging}


def attach_event_workloads(events: Iterable[LogEvent]) -> list[LogEvent]:

    events = list(events)
    workloads = Test.objects.select_related("dev").only(
        "id", "test_mode", "dev_time_control", "dev__name"
    )
    by_id = workloads.in_bulk({event.test_id for event in events})

    for event in events:
        event.workload = by_id.get(event.test_id)

    return events
