from datetime import UTC, datetime, timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.db import connection
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from OpenBench.fleet.machine_detail import load_machine_detail, result_elo
from OpenBench.fleet.machines import (
    CpuGroup,
    MachineRow,
    display_order,
    load_machines_page,
    machine_row,
    merge_cpu_groups,
    summarize_fleet,
)
from OpenBench.fleet.status import (
    OfflineWindow,
    Presence,
    int_of,
    presence,
    relative_age,
    text_of,
)
from OpenBench.fleet.users import latest, load_user_rows
from OpenBench.insights.speed import nodes_per_second
from OpenBench.models import Engine, Machine, Profile, Result, Test
from OpenBench.stats import Elo
from OpenBench.tests.fixtures import create_test, create_user, ensure_book, system_info

NOW = datetime(2026, 9, 30, 12, tzinfo=UTC)
PENTA = (3, 30, 70, 35, 8)


def row(
    id: int,
    cpu: str = "Ryzen",
    threads: int = 4,
    mnps: float = 1.5,
    online: bool = True,
    games: int = 0,
    seen: timedelta = timedelta(),
) -> MachineRow:
    return MachineRow(
        id=id,
        name=None,
        owner="owner",
        cpu_name=cpu,
        isa_name=None,
        os_name=None,
        threads=threads,
        mnps=mnps,
        last_seen=NOW - seen,
        last_seen_ago="just now",
        presence=Presence.ONLINE if online else Presence.OFFLINE,
        workload=None,
        lifetime_games=games,
    )


def make_machine(
    owner: User,
    name: str = "box",
    cpu: str = "Ryzen 9",
    concurrency: int = 4,
    seen: timedelta = timedelta(),
    workload: int = 0,
) -> Machine:
    info = {
        **system_info(concurrency=concurrency),
        "cpu_name": cpu,
        "isa_name": "avx2",
        "machine_name": name,
    }
    machine = Machine.objects.create(user=owner, info=info, mnps=1.5, workload=workload)
    Machine.objects.filter(id=machine.id).update(updated=timezone.now() - seen)
    return Machine.objects.get(id=machine.id)


def add_result(
    test: Test,
    machine: Machine,
    penta: tuple[int, ...] = PENTA,
    nodes: int = 3_000_000,
    time: int = 2000,
) -> Result:
    wins, losses = 2 * penta[4] + penta[3], 2 * penta[0] + penta[1]
    return Result.objects.create(
        test=test,
        machine=machine,
        games=2 * sum(penta),
        wins=wins,
        losses=losses,
        draws=2 * sum(penta) - wins - losses,
        LL=penta[0],
        LD=penta[1],
        DD=penta[2],
        DW=penta[3],
        WW=penta[4],
        dev_nodes=nodes,
        dev_time=time,
    )


def query_count(client, url: str) -> int:
    with CaptureQueriesContext(connection) as context:
        response = client.get(url)
    assert response.status_code == 200, response.status_code
    return len(context.captured_queries)


class StatusTests(SimpleTestCase):
    def test_relative_age(self):
        cases = [
            (0, "just now"),
            (-5, "just now"),
            (42, "42s ago"),
            (60, "1m ago"),
            (3599, "59m ago"),
            (3600, "1h ago"),
            (86399, "23h ago"),
            (86400 * 3 + 5, "3d ago"),
        ]
        for seconds, expected in cases:
            self.assertEqual(
                relative_age(timedelta(seconds=seconds)), expected, seconds
            )

    def test_presence_uses_the_active_machine_window(self):
        self.assertEqual(presence(NOW - timedelta(minutes=2), NOW), Presence.ONLINE)
        self.assertEqual(
            presence(NOW - timedelta(minutes=2, seconds=1), NOW), Presence.OFFLINE
        )

    def test_offline_window_parse_falls_back_to_online_only(self):
        self.assertEqual(OfflineWindow.parse("7d"), OfflineWindow.WEEK)
        self.assertEqual(OfflineWindow.parse(None), OfflineWindow.NONE)
        self.assertEqual(OfflineWindow.parse("forever"), OfflineWindow.NONE)
        self.assertEqual(OfflineWindow.DAY.span, timedelta(days=1))

    def test_info_readers_tolerate_missing_values(self):
        info = {
            "machine_name": "None",
            "cpu_name": "Ryzen",
            "concurrency": "8",
            "bad": "x",
        }
        self.assertIsNone(text_of(info, "machine_name"))
        self.assertIsNone(text_of(info, "absent"))
        self.assertEqual(text_of(info, "cpu_name"), "Ryzen")
        self.assertEqual(
            (int_of(info, "concurrency"), int_of(info, "bad"), int_of(info, "absent")),
            (8, 0, 0),
        )


class FleetAnalyticsTests(SimpleTestCase):
    def test_summary_sums_cpu_groups(self):
        groups = [
            CpuGroup(
                "Ryzen", online=2, machines=3, threads=16, mnps=24.0, lifetime_games=9
            ),
            CpuGroup(
                "Xeon", online=0, machines=4, threads=0, mnps=0.0, lifetime_games=5
            ),
        ]
        summary = summarize_fleet(groups, games_last_24h=1234)
        self.assertEqual(
            (summary.online, summary.offline, summary.machines, summary.threads),
            (2, 5, 7, 16),
        )
        self.assertAlmostEqual(summary.mnps, 24.0)
        self.assertEqual((summary.cpu_models, summary.games_last_24h), (1, 1234))

    def test_merge_cpu_groups_folds_missing_names_into_unknown(self):
        machines = [
            {"cpu": "Ryzen", "online": 1, "machines": 2, "threads": 8, "mnps": 12.0},
            {"cpu": None, "online": 0, "machines": 1, "threads": None, "mnps": None},
            {"cpu": "None", "online": 1, "machines": 1, "threads": 2, "mnps": 2.0},
        ]
        games = [
            {"cpu": "Ryzen", "games": 15},
            {"cpu": None, "games": 4},
            {"cpu": "M4", "games": 1},
        ]
        ryzen, unknown = merge_cpu_groups(machines, games)
        self.assertEqual(ryzen, CpuGroup("Ryzen", 1, 2, 8, 12.0, 15))
        self.assertEqual(unknown, CpuGroup("Unknown", 1, 2, 2, 2.0, 4))

    def test_display_order_puts_online_first_by_cpu_then_offline_by_recency(self):
        rows = [
            row(1, cpu="b", online=False, seen=timedelta(hours=3)),
            row(2, cpu="B"),
            row(3, cpu="a"),
            row(4, cpu="A", online=False, seen=timedelta(hours=1)),
        ]
        self.assertEqual([item.id for item in display_order(rows)], [3, 2, 4, 1])

    def test_machine_row_reads_info(self):
        machine = Machine(
            id=7,
            user=User(username="lab"),
            mnps=2.0,
            updated=NOW - timedelta(minutes=5),
            workload=3,
            info={"machine_name": "fast", "concurrency": 16, "os_name": "Linux"},
        )
        workload = Test(id=3)
        item = machine_row(machine, 42, {3: workload}, NOW)
        self.assertEqual(
            (item.name, item.owner, item.cpu_name, item.threads, item.lifetime_games),
            ("fast", "lab", "Unknown", 16, 42),
        )
        self.assertEqual(
            (item.presence, item.last_seen_ago, item.workload),
            (Presence.OFFLINE, "5m ago", workload),
        )
        self.assertAlmostEqual(item.total_mnps, 32.0)

    def test_latest_ignores_missing_moments(self):
        self.assertIsNone(latest([None, None]))
        self.assertEqual(
            latest([NOW, None, NOW + timedelta(seconds=1)]), NOW + timedelta(seconds=1)
        )

    def test_nodes_per_second(self):
        self.assertEqual(nodes_per_second(3_000_000, 2000), 1_500_000)
        self.assertEqual(nodes_per_second(0, 2000), 0)
        self.assertEqual(nodes_per_second(10, 0), 0)

    def test_result_elo_follows_the_workload_mode(self):
        wins, losses = 2 * PENTA[4] + PENTA[3], 2 * PENTA[0] + PENTA[1]
        counters = {
            "wins": wins,
            "losses": losses,
            "draws": 2 * sum(PENTA) - wins - losses,
            "LL": PENTA[0],
            "LD": PENTA[1],
            "DD": PENTA[2],
            "DW": PENTA[3],
            "WW": PENTA[4],
        }

        penta = result_elo(Result(test=Test(test_mode="SPRT"), **counters))
        self.assertAlmostEqual(penta.value, Elo(PENTA)[1])

        tri = result_elo(Result(test=Test(test_mode="SPRT", use_tri=True), **counters))
        self.assertAlmostEqual(
            tri.value, Elo((counters["losses"], counters["draws"], counters["wins"]))[1]
        )

        self.assertIsNone(result_elo(Result(test=Test(test_mode="SPSA"), **counters)))


class FleetPageTests(TestCase):
    def setUp(self):
        ensure_book()
        self.reader = create_user("reader")
        self.worker = create_user("lab-worker")
        self.test = create_test(self.reader)
        Engine.objects.filter(id=self.test.dev_id).update(name="lmr-tweak")

        self.online = make_machine(
            self.worker, "online-box", concurrency=8, workload=self.test.id
        )
        self.recent = make_machine(
            self.worker, "recent-box", cpu="Apple M4", seen=timedelta(hours=5)
        )
        self.old = make_machine(self.reader, "old-box", seen=timedelta(days=3))
        self.ancient = make_machine(self.reader, "ancient-box", seen=timedelta(days=10))
        add_result(self.test, self.online)
        add_result(self.test, self.recent, (1, 2, 3, 2, 1))

    def login(self):
        self.client.force_login(self.reader)

    def test_anonymous_is_redirected(self):
        for url in ["/machines/", f"/machines/{self.online.id}/", "/users/"]:
            self.assertRedirects(
                self.client.get(url),
                "/login/",
                fetch_redirect_response=False,
                msg_prefix=url,
            )

    def test_machines_lists_online_machines_by_default(self):
        self.login()
        page = self.client.get("/machines/").context["page"]
        self.assertEqual([item.name for item in page.rows], ["online-box"])
        self.assertEqual((page.summary.online, page.summary.threads), (1, 8))
        self.assertEqual(page.rows[0].lifetime_games, 2 * sum(PENTA))

        content = self.client.get("/machines/").content.decode()
        self.assertIn(f'href="/test/{self.test.id}/"', content)
        self.assertIn("lmr-tweak", content)

    def test_machines_window_includes_recently_offline(self):
        self.login()
        day = self.client.get("/machines/?show=24h")
        week = self.client.get("/machines/?show=7d")
        self.assertEqual(
            [item.name for item in day.context["page"].rows],
            ["online-box", "recent-box"],
        )
        self.assertEqual(
            [item.name for item in week.context["page"].rows],
            ["online-box", "recent-box", "old-box"],
        )
        self.assertContains(week, '<span class="badge">Offline</span>', count=2)
        self.assertEqual(week.context["page"].summary.offline, 2)

    def test_lifetime_games_sum_across_workloads(self):
        other = create_test(self.reader)
        add_result(other, self.online, (0, 1, 1, 1, 0))
        page = load_machines_page(timezone.now(), OfflineWindow.WEEK)
        rows = {item.id: item for item in page.rows}
        self.assertEqual(rows[self.online.id].lifetime_games, 2 * sum(PENTA) + 6)
        self.assertEqual(rows[self.old.id].lifetime_games, 0)

    def test_machines_listing_caps_offline_rows(self):
        newest = [
            make_machine(self.worker, f"gone-{hours}", seen=timedelta(hours=hours))
            for hours in (1, 2, 3, 4)
        ]
        page = load_machines_page(timezone.now(), OfflineWindow.WEEK, offline_limit=3)

        self.assertEqual(
            [item.id for item in page.rows],
            [self.online.id, *(machine.id for machine in newest[:3])],
        )
        self.assertTrue(page.truncated)
        self.assertEqual((page.offline_listed, page.summary.offline), (3, 6))
        self.assertEqual(sum(group.machines for group in page.cpus), 7)
        self.assertEqual(
            {group.cpu_name: group.lifetime_games for group in page.cpus},
            {"Ryzen 9": 2 * sum(PENTA), "Apple M4": 18},
        )

        self.login()
        with mock.patch("OpenBench.fleet.machines.OFFLINE_LISTED", 2):
            response = self.client.get("/machines/?show=7d")
        self.assertEqual(len(response.context["page"].rows), 3)
        self.assertContains(response, "the 2 most recently seen of 6 offline machines")

    def test_machines_query_count_is_bounded(self):
        self.login()
        for index in range(8):
            host = make_machine(
                self.worker,
                f"extra-{index}",
                cpu=f"CPU {index}",
                workload=create_test(self.reader).id,
            )
            add_result(self.test, host)

        with self.assertNumQueries(13):
            self.client.get("/machines/?show=7d")

        for index in range(8):
            make_machine(
                self.reader, f"more-{index}", workload=create_test(self.reader).id
            )

        self.assertEqual(query_count(self.client, "/machines/?show=7d"), 13)

    def test_machine_detail(self):
        self.login()
        spsa = create_test(self.reader, test_mode="SPSA")
        add_result(spsa, self.online)

        response = self.client.get(f"/machines/{self.online.id}/")
        detail = response.context["detail"]
        self.assertTrue(detail.row.online)
        self.assertEqual(detail.row.workload, self.test)
        self.assertEqual(detail.workloads_total, 2)
        self.assertEqual(
            [item.test.id for item in detail.contributions], [spsa.id, self.test.id]
        )
        self.assertIsNone(detail.contributions[0].elo)
        self.assertAlmostEqual(detail.contributions[1].elo.value, Elo(PENTA)[1])
        self.assertEqual(detail.contributions[1].nps, 1_500_000)
        self.assertContains(response, "Current workload")
        self.assertContains(response, "1,500,000")

    def test_machine_detail_is_bounded(self):
        for _ in range(3):
            add_result(create_test(self.reader), self.old)

        detail = load_machine_detail(self.old.id, timezone.now(), limit=2)
        self.assertEqual(
            (len(detail.contributions), detail.workloads_total, detail.truncated),
            (2, 3, True),
        )
        self.assertFalse(detail.row.online)

    def test_unknown_machine_redirects(self):
        self.login()
        self.assertRedirects(
            self.client.get("/machines/999999/"),
            "/machines/",
            fetch_redirect_response=False,
        )

    def test_users_page(self):
        self.login()
        Profile.objects.filter(user=self.worker).update(games=500)
        Profile.objects.filter(user=self.reader).update(tests=2)
        create_user("idle")

        rows = {
            item.username: item for item in self.client.get("/users/").context["rows"]
        }
        self.assertEqual(set(rows), {"lab-worker", "reader"})
        self.assertEqual(
            (rows["lab-worker"].machines_online, rows["lab-worker"].threads_online),
            (1, 8),
        )
        self.assertEqual(
            (rows["reader"].machines_online, rows["reader"].threads_online), (0, 0)
        )
        self.assertLess(
            timezone.now() - rows["lab-worker"].last_activity, timedelta(minutes=1)
        )
        self.assertLess(
            timezone.now() - rows["reader"].last_activity, timedelta(minutes=1)
        )

    def test_last_activity_ignores_logins(self):
        idle = create_user("idle", approver=True)
        self.client.force_login(idle)
        rows = {item.username: item for item in load_user_rows(timezone.now())}
        self.assertIsNone(rows["idle"].last_activity)

    def test_user_with_only_an_online_machine_is_listed(self):
        rows = {item.username: item for item in load_user_rows(timezone.now())}
        self.assertIn("lab-worker", rows)
        self.assertEqual(rows["lab-worker"].games, 0)

    def test_users_query_count_is_bounded(self):
        self.login()
        for index in range(6):
            owner = create_user(f"user-{index}")
            Profile.objects.filter(user=owner).update(games=index + 1)
            make_machine(owner, f"box-{index}")

        with self.assertNumQueries(10):
            self.client.get("/users/")

        for index in range(6, 12):
            owner = create_user(f"user-{index}")
            Profile.objects.filter(user=owner).update(games=index + 1)
            make_machine(owner, f"box-{index}", seen=timedelta(hours=1))

        self.assertEqual(query_count(self.client, "/users/"), 10)
