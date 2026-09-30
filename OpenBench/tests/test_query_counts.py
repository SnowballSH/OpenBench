import dataclasses
import re
from typing import ClassVar

from django.test import TestCase

from OpenBench.models import LogEvent, Machine, Network, SPSAParameter, SPSARun, Test
from OpenBench.page_queries import listing_tests
from OpenBench.templatetags.mytags import prettyDevName, shortStatBlock
from OpenBench.tests.datasets import SMALL, Dataset, DatasetSize, build_dataset
from OpenBench.tests.fixtures import (
    create_engine_config,
    create_test,
    create_user,
    ensure_book,
    register_payload,
    system_info,
)
from OpenBench.utils import getMachineStatus

MEDIUM = dataclasses.replace(
    SMALL,
    users=8,
    tests=160,
    machines=400,
    results=1200,
    events=300,
    networks=40,
    pgns=200,
)

PAGE_QUERIES = {
    "/index/": 9,
    "/index/2/": 6,
    "/user/user1/": 9,
    "/greens/": 6,
    "/search/?keywords=branch": 7,
    "/search/2/?authors=user1+user2": 7,
    "/events/": 7,
    "/errors/": 7,
    "/networks/": 5,
    "/api/insights/server/": 11,
}

WORKLOAD_QUERIES = {
    "/test/{}/": 11,
    "/api/workload/{}/summary/": 5,
    "/api/workload/{}/results/": 5,
    "/api/workload/{}/insights/": 6,
}


class QueryBudgetTests(TestCase):
    # The same budgets hold for every size, so no page costs a query per row
    size: ClassVar[DatasetSize] = SMALL
    data: ClassVar[Dataset]

    @classmethod
    def setUpTestData(cls) -> None:
        cls.data = build_dataset(cls.size)

    def setUp(self) -> None:
        self.client.force_login(self.data.users[0])

    def assert_page_queries(self, url: str, queries: int) -> None:
        with self.subTest(url=url), self.assertNumQueries(queries):
            self.assertEqual(self.client.get(url).status_code, 200)

    def test_pages(self) -> None:
        for url, queries in PAGE_QUERIES.items():
            self.assert_page_queries(url, queries)

    def test_workload_pages(self) -> None:
        for url, queries in WORKLOAD_QUERIES.items():
            self.assert_page_queries(url.format(self.data.workload.id), queries)


class LargerQueryBudgetTests(QueryBudgetTests):
    size = MEDIUM


class SearchPagingTests(TestCase):
    def setUp(self) -> None:
        create_engine_config()
        ensure_book()
        self.author = create_user("author")
        self.client.force_login(self.author)
        self.tests = [
            create_test(self.author, info=f"match {index}") for index in range(30)
        ]

    def shown_ids(self, url: str) -> list[int]:
        content = self.client.get(url).content.decode()
        return [
            int(test_id) for test_id in re.findall(r'<a href="/test/(\d+)/">', content)
        ]

    def test_pages_list_the_newest_matches_first(self) -> None:
        newest = [test.id for test in reversed(self.tests)]
        self.assertEqual(self.shown_ids("/search/?info-contains=match"), newest[:25])
        self.assertEqual(self.shown_ids("/search/2/?info-contains=match"), newest[25:])

    def test_page_links_carry_the_search(self) -> None:
        content = self.client.get(
            "/search/?info-contains=match&hide-reds=on"
        ).content.decode()
        self.assertIn('href="/search/2/?info-contains=match&amp;hide-reds=on"', content)

    def test_no_matches_reports_an_error(self) -> None:
        self.assertContains(
            self.client.get("/search/?info-contains=nothing"), "No matching tests found"
        )


class EventWorkloadTests(TestCase):
    def test_events_name_their_workload(self) -> None:
        create_engine_config()
        ensure_book()
        author = create_user("author")
        test = create_test(author, test_mode="SPSA", dev_time_control="N=25000")
        LogEvent.objects.create(
            author="author", summary="STOP", log_file="", test_id=test.id
        )
        self.client.force_login(author)

        content = self.client.get("/events/").content.decode()

        self.assertIn(f'<a href="/tune/{test.id}/">dev</a>', content)
        self.assertIn('<td class="mono">N=25000</td>', content)


class ListingAnnotationTests(TestCase):
    def setUp(self) -> None:
        create_engine_config()
        ensure_book()
        self.author = create_user("author")

    def listed(self, test: Test) -> Test:
        return listing_tests(Test.objects.filter(id=test.id)).get()

    def test_network_names_match_the_unannotated_lookup(self) -> None:
        Network.objects.create(
            sha256="AAAAAAAA", name="renamed", engine="Avalanche", author="author"
        )
        named = create_test(
            self.author,
            dev_network="AAAAAAAA",
            base_network="BBBBBBBB",
            dev_netname="mine",
        )
        missing = create_test(
            self.author,
            dev_network="CCCCCCCC",
            base_network="BBBBBBBB",
            dev_netname="mine",
        )

        for test in (named, missing):
            Test.objects.filter(id=test.id).update(base_id=test.dev_id)
            plain = Test.objects.select_related("dev", "base").get(id=test.id)
            listed = self.listed(test)
            with self.assertNumQueries(0):
                listed_name = prettyDevName(listed)
            self.assertEqual(listed_name, prettyDevName(plain))

        self.assertEqual(prettyDevName(self.listed(named)), "renamed")
        self.assertEqual(prettyDevName(self.listed(missing)), "mine")

    def test_tune_blocks_match_the_unannotated_count(self) -> None:
        tune = create_test(self.author, test_mode="SPSA", games=64)
        run = SPSARun.objects.create(
            tune=tune,
            reporting_type="BULK",
            distribution_type="SINGLE",
            alpha=0.602,
            gamma=0.101,
            iterations=100,
            pairs_per=8,
            a_ratio=0.1,
        )
        SPSAParameter.objects.bulk_create(
            [
                SPSAParameter(
                    spsa_run=run,
                    name=f"P{index}",
                    index=index,
                    value=1,
                    is_float=False,
                    start=1,
                    min_value=0,
                    max_value=2,
                    c_end=1,
                    r_end=0.002,
                    c_value=1,
                    a_value=1,
                )
                for index in range(3)
            ]
        )

        listed = self.listed(tune)
        with self.assertNumQueries(0):
            block = shortStatBlock(listed)

        self.assertEqual(block, shortStatBlock(Test.objects.get(id=tune.id)))
        self.assertIn("Tuning 3 Parameters", block)


class MachineStatusTests(TestCase):
    def test_sums_recent_machines(self) -> None:
        owner, other = create_user("owner"), create_user("other")
        Machine.objects.create(user=owner, info=system_info(concurrency=4), mnps=1.5)
        Machine.objects.create(user=other, info=system_info(concurrency=8), mnps=2.0)

        self.assertEqual(getMachineStatus(), ": 2 Machines / 12 Threads / 22.0 MNPS ")
        self.assertEqual(
            getMachineStatus("owner"), ": 1 Machines / 4 Threads / 6.0 MNPS "
        )

    def test_nothing_online(self) -> None:
        self.assertEqual(getMachineStatus(), ": 0 Machines / 0 Threads / 0 MNPS ")

    def test_online_machines_without_nps_yet(self) -> None:
        Machine.objects.create(
            user=create_user("owner"), info=system_info(concurrency=4)
        )
        self.assertEqual(getMachineStatus(), ": 1 Machines / 4 Threads / 0.0 MNPS ")


class ClientEndpointBudgetTests(TestCase):
    def setUp(self) -> None:
        create_engine_config()
        ensure_book()
        self.worker = create_user("lab-worker")
        self.test = create_test(create_user("admin", approver=True))
        registered = self.client.post(
            "/clientWorkerInfo/", register_payload(self.worker)
        ).json()
        self.session = {
            "machine_id": registered["machine_id"],
            "secret": registered["secret"],
        }

    def post(self, url: str, payload: dict[str, object], queries: int) -> object:
        with self.assertNumQueries(queries):
            return self.client.post(url, payload).json()

    def test_worker_loop(self) -> None:
        workload = self.post("/clientGetWorkload/", self.session, 15)["workload"]
        self.post("/clientGetWorkload/", self.session, 12)

        results = {
            **self.session,
            "test_id": self.test.id,
            "result_id": workload["result"]["id"],
            "crashes": 0,
            "timelosses": 0,
            "illegals": 0,
            "trinomial": "1 2 3",
            "pentanomial": "0 1 1 1 0",
        }
        self.assertEqual(self.post("/clientSubmitResults/", results, 14), {})
        self.post("/clientHeartbeat/", {**self.session, "test_id": self.test.id}, 4)
