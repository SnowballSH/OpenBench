import io
import json
import uuid
import zlib
from datetime import UTC, datetime, timedelta
from importlib import import_module
from unittest import mock

from django.apps import apps
from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import DatabaseError, connection
from django.db.models import F
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from OpenBench.fleet.hosts import HostKey, host_identity, host_key, stable_mac
from OpenBench.fleet.housekeeping import (
    EXITED_AFTER,
    apply_prune,
    exits_when_idle,
    plan_prune,
    prune_exited_sessions,
    rekey_unkeyed,
)
from OpenBench.fleet.machine_detail import load_host_detail, pooled_elo
from OpenBench.fleet.machines import (
    CpuGroup,
    CurrentSession,
    HostRow,
    HostTotals,
    MachinesPage,
    PoolRow,
    cpu_groups,
    host_row,
    listed,
    load_machines_page,
    summarize_fleet,
)
from OpenBench.fleet.pools import Pool, parse_pool_key, pool_label, short_name
from OpenBench.fleet.sessions import current_sessions, never_used
from OpenBench.fleet.status import (
    OfflineWindow,
    Presence,
    presence,
    relative_age,
)
from OpenBench.fleet.users import latest, load_user_rows
from OpenBench.insights.domain import Outcomes
from OpenBench.insights.server import load_fleet
from OpenBench.insights.sources import result_rows
from OpenBench.insights.speed import nodes_per_second
from OpenBench.machine_info import int_of, text_of
from OpenBench.models import Engine, Machine, Profile, Result, Test
from OpenBench.stats import Elo
from OpenBench.tests.fixtures import (
    create_engine_config,
    create_test,
    create_user,
    ensure_book,
    present,
    register_payload,
    system_info,
)
from OpenBench.tests.test_listing_rows import row_audit
from OpenBench.utils import getMachineStatus

NOW = datetime(2026, 9, 30, 12, tzinfo=UTC)
PENTA = (3, 30, 70, 35, 8)


def row(
    id: int,
    cpu: str = 'Ryzen',
    threads: int = 4,
    mnps: float = 1.5,
    online: bool = True,
    games: int = 0,
    seen: timedelta = timedelta(),
    name: str | None = None,
) -> HostRow:
    return HostRow(
        key=HostKey(f'host-{id}'),
        machine_id=id,
        name=name,
        owner='owner',
        cpu_name=cpu,
        isa_name=None,
        os_name=None,
        threads=threads,
        mnps=mnps,
        first_seen=NOW - seen,
        last_seen=NOW - seen,
        last_seen_ago='just now',
        presence=Presence.ONLINE if online else Presence.OFFLINE,
        workload_id=0,
        workload=None,
        sessions=1,
        lifetime_games=games,
    )


JOB_IDS = (
    'a0741747-7d81-49a2-b96e-4b14d4c304c3',
    '5b1e0c52-93aa-4f0e-8a67-0d2f6c1e9b11',
    'c97d2a10-0b6f-4d3c-b5e4-7a1f33e0d2aa',
    '0e4f9b7c-61d2-4a85-9c3b-f2a7d5e81c46',
)


def mac_of(name: str) -> str:
    return f'{zlib.crc32(name.encode()):012X}'


def make_machine(
    owner: User,
    name: str = 'box',
    cpu: str = 'Ryzen 9',
    concurrency: int = 4,
    seen: timedelta = timedelta(),
    workload: int = 0,
    cli_options: str = '',
) -> Machine:
    info = {
        **system_info(concurrency=concurrency),
        'logical_cores': 16,
        'cpu_name': cpu,
        'isa_name': 'avx2',
        'machine_name': name,
        'mac_address': mac_of(name),
        'cli_options': cli_options,
    }
    machine = Machine.objects.create(user=owner, info=info, mnps=1.5, workload=workload)
    Machine.objects.filter(id=machine.id).update(updated=timezone.now() - seen)
    return Machine.objects.get(id=machine.id)


SUPERVISED = '--threads 4 --identity box --single_workload'


def make_registration(owner: User, of: Machine, minutes: float, cli_options: str = SUPERVISED) -> Machine:
    machine = Machine.objects.create(user=owner, info={**of.info, 'cli_options': cli_options})
    Machine.objects.filter(id=machine.id).update(updated=timezone.now() - timedelta(minutes=minutes))
    return machine


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


def host_rows(page: MachinesPage) -> list[HostRow]:
    return [item for item in page.rows if isinstance(item, HostRow)]


def query_count(client, url: str) -> int:
    with CaptureQueriesContext(connection) as context:
        response = client.get(url)
    assert response.status_code == 200, response.status_code
    return len(context.captured_queries)


class StatusTests(SimpleTestCase):
    def test_relative_age(self):
        cases = [
            (0, 'just now'),
            (-5, 'just now'),
            (42, '42s ago'),
            (60, '1m ago'),
            (3599, '59m ago'),
            (3600, '1h ago'),
            (86399, '23h ago'),
            (86400 * 3 + 5, '3d ago'),
        ]
        for seconds, expected in cases:
            self.assertEqual(relative_age(timedelta(seconds=seconds)), expected, seconds)

    def test_presence_uses_the_active_machine_window(self):
        self.assertEqual(presence(NOW - timedelta(minutes=2), NOW), Presence.ONLINE)
        self.assertEqual(presence(NOW - timedelta(minutes=2, seconds=1), NOW), Presence.OFFLINE)

    def test_offline_window_parse_falls_back_to_online_only(self):
        self.assertEqual(OfflineWindow.parse('7d'), OfflineWindow.WEEK)
        self.assertEqual(OfflineWindow.parse(None), OfflineWindow.NONE)
        self.assertEqual(OfflineWindow.parse('forever'), OfflineWindow.NONE)
        self.assertEqual(OfflineWindow.DAY.span, timedelta(days=1))

    def test_info_readers_tolerate_missing_values(self):
        info = {
            'machine_name': 'None',
            'cpu_name': 'Ryzen',
            'concurrency': '8',
            'bad': 'x',
        }
        self.assertIsNone(text_of(info, 'machine_name'))
        self.assertIsNone(text_of(info, 'absent'))
        self.assertEqual(text_of(info, 'cpu_name'), 'Ryzen')
        self.assertEqual(
            (int_of(info, 'concurrency'), int_of(info, 'bad'), int_of(info, 'absent')),
            (8, 0, 0),
        )


class FleetAnalyticsTests(SimpleTestCase):
    def test_summary_sums_cpu_groups(self):
        groups = [
            CpuGroup('Ryzen', online=2, hosts=3, threads=16, mnps=24.0, lifetime_games=9),
            CpuGroup('Xeon', online=0, hosts=4, threads=0, mnps=0.0, lifetime_games=5),
        ]
        summary = summarize_fleet(groups, games_last_24h=1234)
        self.assertEqual(
            (summary.online, summary.offline, summary.hosts, summary.threads),
            (2, 5, 7, 16),
        )
        self.assertAlmostEqual(summary.mnps, 24.0)
        self.assertEqual((summary.cpu_models, summary.games_last_24h), (1, 1234))

    def test_cpu_groups_count_threads_of_online_hosts_only(self):
        rows = [
            row(1, cpu='Ryzen', threads=8, games=10),
            row(2, cpu='Ryzen', threads=4, online=False, games=5),
            row(3, cpu='M4', threads=2, mnps=1.0, games=1),
        ]
        ryzen, m4 = cpu_groups(rows)
        self.assertEqual(ryzen, CpuGroup('Ryzen', 1, 2, 8, 12.0, 15))
        self.assertEqual(m4, CpuGroup('M4', 1, 1, 2, 2.0, 1))

    def test_listing_puts_online_first_by_cpu_then_offline_by_recency(self):
        rows = [
            row(1, cpu='b', online=False, seen=timedelta(hours=3)),
            row(2, cpu='B'),
            row(3, cpu='a'),
            row(4, cpu='A', online=False, seen=timedelta(hours=1)),
        ]
        self.assertEqual(
            [item.machine_id for item in listed(rows, 5, None, NOW) if isinstance(item, HostRow)], [3, 2, 4, 1]
        )
        self.assertEqual(len(listed(rows, 1, None, NOW)), 3)

    def test_offline_hosts_of_a_pool_roll_up_into_one_row(self):
        jobs = [
            row(
                index,
                cpu='EPYC',
                online=False,
                games=10 * index,
                seen=timedelta(hours=index),
                name=f'batch-{JOB_IDS[index]}:0',
            )
            for index in (1, 2, 3)
        ]
        running = row(4, cpu='EPYC', name=f'batch-{JOB_IDS[0]}:0')
        other_cpu = row(5, cpu='Xeon', online=False, seen=timedelta(hours=9), name=f'batch-{JOB_IDS[0]}:1')
        stable = row(6, cpu='EPYC', online=False, seen=timedelta(hours=2), name='demo-3')

        shown = listed([*jobs, running, other_cpu, stable], 10, None, NOW)

        self.assertEqual([item.name for item in shown], [running.name, 'batch-*', 'demo-3', other_cpu.name])
        pool = shown[1]
        assert isinstance(pool, PoolRow)
        self.assertEqual(
            (pool.hosts, pool.sessions, pool.lifetime_games, pool.owner, pool.cpu_name), (3, 3, 60, 'owner', 'EPYC')
        )
        self.assertEqual((pool.last_seen, pool.first_seen), (NOW - timedelta(hours=1), NOW - timedelta(hours=3)))
        self.assertEqual(
            (pool.last_seen_ago, pool.is_pool, pool.online, running.is_pool), ('1h ago', True, False, False)
        )
        self.assertEqual(pool.key, Pool('owner', 'batch-*', 'EPYC').key)

        expanded = listed([*jobs, running, other_cpu, stable], 10, pool.key, NOW)
        self.assertEqual([item.hosts for item in expanded], [1] * 6)
        self.assertEqual(jobs[0].label, f'batch-{JOB_IDS[1][:8]}…:0')

    def test_host_row_reads_the_current_session(self):
        session = CurrentSession(
            id=7,
            host_key='abc',
            owner='lab',
            mnps=2.0,
            updated=NOW - timedelta(minutes=5),
            workload=3,
            name='fast',
            cpu='None',
            isa=None,
            os='Linux',
            threads=16,
        )
        item = host_row(session, HostTotals(sessions=4, first_seen=NOW - timedelta(days=2), lifetime_games=42), NOW)
        self.assertEqual(
            (item.name, item.owner, item.cpu_name, item.threads, item.lifetime_games, item.sessions),
            ('fast', 'lab', 'Unknown', 16, 42, 4),
        )
        self.assertEqual(
            (item.presence, item.last_seen_ago, item.workload_id, item.first_seen),
            (Presence.OFFLINE, '5m ago', 3, NOW - timedelta(days=2)),
        )
        self.assertAlmostEqual(item.total_mnps, 32.0)

        unseen = host_row(session, None, NOW)
        self.assertEqual((unseen.sessions, unseen.first_seen, unseen.lifetime_games), (1, session['updated'], 0))

    def test_latest_ignores_missing_moments(self):
        self.assertIsNone(latest([None, None]))
        self.assertEqual(latest([NOW, None, NOW + timedelta(seconds=1)]), NOW + timedelta(seconds=1))

    def test_nodes_per_second(self):
        self.assertEqual(nodes_per_second(3_000_000, 2000), 1_500_000)
        self.assertEqual(nodes_per_second(0, 2000), 0)
        self.assertEqual(nodes_per_second(10, 0), 0)

    def test_pooled_elo_follows_the_workload_mode(self):
        wins, losses = 2 * PENTA[4] + PENTA[3], 2 * PENTA[0] + PENTA[1]
        trinomial = (losses, 2 * sum(PENTA) - wins - losses, wins)

        penta = present(pooled_elo(Test(test_mode='SPRT'), Outcomes(trinomial, PENTA, True)))
        self.assertAlmostEqual(penta.value, Elo(PENTA)[1])

        tri = present(pooled_elo(Test(test_mode='SPRT'), Outcomes(trinomial, PENTA, False)))
        self.assertAlmostEqual(tri.value, Elo(trinomial)[1])

        self.assertIsNone(pooled_elo(Test(test_mode='SPSA'), Outcomes(trinomial, PENTA, True)))


class HostIdentityTests(SimpleTestCase):
    INFO = {  # noqa: RUF012
        'mac_address': 'A4B1C2D3E4F5',
        'machine_name': 'box',
        'cpu_name': 'Ryzen 9',
        'os_name': 'Linux',
        'logical_cores': 32,
        'physical_cores': 16,
    }

    def key(self, owner: str = 'lab', **overrides: object) -> HostKey:
        return host_key(owner, {**self.INFO, **overrides})

    def test_registrations_of_one_computer_share_a_key(self):
        again = self.key(concurrency=8, cli_options='--single_workload', os_ver='6.9', client_ver=51)
        self.assertEqual(self.key(), again)
        self.assertRegex(self.key(), r'^[0-9a-f]{32}$')

    def test_owners_never_merge(self):
        self.assertNotEqual(self.key('lab'), self.key('home'))
        self.assertNotEqual(self.key('lab', machine_name=None), self.key('home', machine_name=None))
        self.assertNotEqual(
            self.key('lab', machine_name=None, mac_address=None), self.key('home', machine_name=None, mac_address=None)
        )

    def test_a_name_outranks_the_address(self):
        for address in ('86A1B2C3D4E5', '3DA1B2C3D4E5', 'EF0000000001', None, 'None', ''):
            self.assertEqual(self.key(), self.key(mac_address=address), address)
        self.assertNotEqual(self.key(), self.key(machine_name='other'))
        self.assertNotEqual(self.key(), self.key(cpu_name='Xeon'))
        self.assertNotEqual(self.key(), self.key(logical_cores=64))
        self.assertNotEqual(self.key(), self.key(os_name='Windows'))

    def test_an_unnamed_machine_is_known_by_its_address_and_hardware(self):
        unnamed = self.key(machine_name='None')
        self.assertEqual(unnamed, self.key(machine_name=None))
        self.assertEqual(unnamed, self.key(machine_name='', mac_address='a4b1c2d3e4f5'))
        self.assertNotEqual(unnamed, self.key())
        self.assertNotEqual(unnamed, self.key(machine_name=None, mac_address='86A1B2C3D4E5'))
        self.assertNotEqual(unnamed, self.key(machine_name=None, cpu_name='Xeon'))

    def test_an_unnamed_machine_without_an_address_has_only_its_hardware(self):
        bare = self.key(machine_name=None, mac_address=None)
        self.assertEqual(bare, self.key(machine_name='None', mac_address='A5B1C2D3E4F5'))
        self.assertNotEqual(bare, self.key(machine_name=None))
        self.assertNotEqual(bare, self.key(machine_name=None, mac_address=None, physical_cores=8))

    def test_random_and_malformed_addresses_are_ignored(self):
        self.assertEqual(stable_mac('a4b1c2d3e4f5'), 'A4B1C2D3E4F5')
        self.assertEqual(stable_mac('2B1C2D3E4F5'), '02B1C2D3E4F5')
        for value in (
            'A5B1C2D3E4F5',
            '0',
            '',
            'None',
            None,
            'not-hex',
            '1A4B1C2D3E4F5',
            17,
            ['A4B1C2D3E4F5'],
            '0XA4B1C2D3E4',
            'A4_B1',
            ' A4B1C2D3E4F5',
            'A4B1C2D3E4F5\n',
            '-A4B1',
            '+A4B1',
        ):
            self.assertIsNone(stable_mac(value), value)
        self.assertEqual(self.key(mac_address='A5B1C2D3E4F5'), self.key(mac_address='01B1C2D3E4F5'))

    def test_identity_tolerates_malformed_info(self):
        malformed: tuple[object, ...] = (None, [], 'text', {'logical_cores': 'many', 'cpu_name': 7})
        for info in malformed:
            self.assertRegex(host_key('lab', info), r'^[0-9a-f]{32}$')
        self.assertEqual(host_identity('lab', None).logical_cores, 0)
        self.assertEqual(host_identity('lab', {'cpu_name': 7}).cpu_name, '7')

    def test_a_batch_job_is_one_host_whatever_address_each_start_reports(self):
        job = {**self.INFO, 'machine_name': 'batch-749476a3-7d81-49a2-b96e-4b14d4c304c3:0', 'cpu_name': 'AMD EPYC 9R14'}
        starts = [{**job, 'mac_address': f'{octet}A1B2C3D4E5'} for octet in ('86', '3D', 'EF', 'AF', '85', '97')]
        self.assertEqual(len({host_key('lab-worker', start) for start in starts}), 1)

        slot = {**starts[0], 'machine_name': 'batch-749476a3-7d81-49a2-b96e-4b14d4c304c3:1'}
        other_job = {**starts[0], 'machine_name': f'batch-{JOB_IDS[1]}:0'}
        keys = {host_key('lab-worker', info) for info in (starts[0], slot, other_job)}
        self.assertEqual(len(keys), 3)
        labels = {pool_label(text_of(info, 'machine_name'), 'AMD EPYC 9R14') for info in (starts[0], slot, other_job)}
        self.assertEqual(labels, {'batch-*'})

    def test_pool_labels_collapse_the_volatile_parts_of_a_name(self):
        cases = {
            f'batch-{JOB_IDS[0]}:0': 'batch-*',
            f'batch-{JOB_IDS[1]}': 'batch-*',
            f'batch-{JOB_IDS[2].upper()}:12': 'batch-*',
            'demo-3': 'demo-3',
            'box': 'box',
            'ip-10-0-12-34': 'ip-10-0-12-34',
            'worker-0a1b2c3d4e': 'worker-*',
            'i-0f21e8465a561ec9e': 'i-*',
            'node00123': 'node*',
            'run-20260930-123456:3': 'run-*',
            'deadbeefcafe': 'deadbeefcafe',
            'defaced-box:1': 'defaced-box',
        }
        for name, label in cases.items():
            self.assertEqual(pool_label(name, 'AMD EPYC 9R14'), label, name)
        self.assertEqual(pool_label(None, 'AMD EPYC 9R14'), 'AMD EPYC 9R14')
        self.assertEqual(pool_label(None, None), 'Unknown')

    def test_short_names_keep_the_start_of_a_long_id(self):
        self.assertEqual(short_name(f'batch-{JOB_IDS[0]}:0'), 'batch-a0741747…:0')
        self.assertEqual(short_name('i-0f21e8465a561ec9e'), 'i-0f21e846…')
        self.assertEqual(short_name('demo-3'), 'demo-3')

    def test_pool_keys_separate_owners_and_cpus_and_reject_other_input(self):
        key = Pool('lab-worker', 'batch-*', 'AMD EPYC 9R14').key
        self.assertRegex(key, r'^[0-9a-f]{12}$')
        self.assertNotEqual(key, Pool('home-worker', 'batch-*', 'AMD EPYC 9R14').key)
        self.assertNotEqual(key, Pool('lab-worker', 'batch-*', 'AMD EPYC 9R45').key)
        self.assertEqual(parse_pool_key(key), key)
        for value in (None, '', 'batch-*', key.upper(), key + '0', "' OR 1=1"):
            self.assertIsNone(parse_pool_key(value), value)

    def test_housekeeping_reads_the_exit_flags_as_whole_options(self):
        self.assertTrue(exits_when_idle('--threads 4 --single_workload'))
        self.assertTrue(exits_when_idle('--fleet --threads 4'))
        for options in ('--threads 4', '--identity my--fleet', '--identity --fleetwood', '', None, 4):
            self.assertFalse(exits_when_idle(options), options)


class FleetPageTests(TestCase):
    def setUp(self):
        ensure_book()
        self.reader = create_user('reader')
        self.worker = create_user('lab-worker')
        self.test = create_test(self.reader)
        Engine.objects.filter(id=self.test.dev_id).update(name='lmr-tweak')

        self.online = make_machine(self.worker, 'online-box', concurrency=8, workload=self.test.id)
        self.recent = make_machine(self.worker, 'recent-box', cpu='Apple M4', seen=timedelta(hours=5))
        self.old = make_machine(self.reader, 'old-box', seen=timedelta(days=3))
        self.ancient = make_machine(self.reader, 'ancient-box', seen=timedelta(days=10))
        add_result(self.test, self.online)
        add_result(self.test, self.recent, (1, 2, 3, 2, 1))

    def login(self):
        self.client.force_login(self.reader)

    def test_anonymous_is_redirected(self):
        for url in ['/machines/', f'/machines/{self.online.id}/', '/users/']:
            self.assertRedirects(
                self.client.get(url),
                '/login/',
                fetch_redirect_response=False,
                msg_prefix=url,
            )

    def test_machines_lists_online_machines_by_default(self):
        self.login()
        page = self.client.get('/machines/').context['page']
        self.assertEqual([item.name for item in page.rows], ['online-box'])
        self.assertEqual((page.summary.online, page.summary.threads), (1, 8))
        self.assertEqual(page.rows[0].lifetime_games, 2 * sum(PENTA))

        content = self.client.get('/machines/').content.decode()
        self.assertIn(f'href="/test/{self.test.id}/"', content)
        self.assertIn('lmr-tweak', content)

    def test_machines_window_includes_recently_offline(self):
        self.login()
        day = self.client.get('/machines/?show=24h')
        week = self.client.get('/machines/?show=7d')
        self.assertEqual(
            [item.name for item in day.context['page'].rows],
            ['online-box', 'recent-box'],
        )
        self.assertEqual(
            [item.name for item in week.context['page'].rows],
            ['online-box', 'recent-box', 'old-box'],
        )
        self.assertContains(week, '<span class="badge">Offline</span>', count=2)
        self.assertEqual(week.context['page'].summary.offline, 2)

    def test_lifetime_games_sum_across_workloads(self):
        other = create_test(self.reader)
        add_result(other, self.online, (0, 1, 1, 1, 0))
        page = load_machines_page(timezone.now(), OfflineWindow.WEEK)
        rows = {item.machine_id: item for item in host_rows(page)}
        self.assertEqual(rows[self.online.id].lifetime_games, 2 * sum(PENTA) + 6)
        self.assertEqual(rows[self.old.id].lifetime_games, 0)

    def test_machines_listing_caps_offline_rows(self):
        newest = [make_machine(self.worker, f'gone-{hours}', seen=timedelta(hours=hours)) for hours in (1, 2, 3, 4)]
        page = load_machines_page(timezone.now(), OfflineWindow.WEEK, offline_limit=3)

        self.assertEqual(
            [item.machine_id for item in host_rows(page)],
            [self.online.id, *(machine.id for machine in newest[:3])],
        )
        self.assertTrue(page.truncated)
        self.assertEqual((page.offline_listed, page.summary.offline), (3, 6))
        self.assertEqual(sum(group.hosts for group in page.cpus), 7)
        self.assertEqual(
            {group.cpu_name: group.lifetime_games for group in page.cpus},
            {'Ryzen 9': 2 * sum(PENTA), 'Apple M4': 18},
        )

        self.login()
        with mock.patch('OpenBench.fleet.machines.OFFLINE_LISTED', 2):
            response = self.client.get('/machines/?show=7d')
        self.assertEqual(len(response.context['page'].rows), 3)
        self.assertContains(response, 'the 2 most recently seen of 6 offline machines')

    def test_machines_query_count_is_bounded(self):
        self.login()
        for index in range(8):
            host = make_machine(
                self.worker,
                f'extra-{index}',
                cpu=f'CPU {index}',
                workload=create_test(self.reader).id,
            )
            add_result(self.test, host)

        with self.assertNumQueries(9):
            self.client.get('/machines/?show=7d')

        for index in range(8):
            make_machine(self.reader, f'more-{index}', workload=create_test(self.reader).id)

        self.assertEqual(query_count(self.client, '/machines/?show=7d'), 9)

    def test_machine_detail(self):
        self.login()
        spsa = create_test(self.reader, test_mode='SPSA')
        add_result(spsa, self.online)

        response = self.client.get(f'/machines/{self.online.id}/')
        detail = response.context['detail']
        self.assertTrue(detail.host.online)
        self.assertEqual(detail.host.workload, self.test)
        self.assertEqual((detail.workloads_total, detail.sessions_total), (2, 1))
        self.assertEqual([item.test.id for item in detail.contributions], [spsa.id, self.test.id])
        self.assertIsNone(detail.contributions[0].elo)
        self.assertAlmostEqual(detail.contributions[1].elo.value, Elo(PENTA)[1])
        self.assertEqual(detail.contributions[1].nps, 1_500_000)
        self.assertContains(response, 'Current workload')
        self.assertContains(response, '1,500,000')

    def test_machine_detail_is_bounded(self):
        for _ in range(3):
            add_result(create_test(self.reader), self.old)

        detail = present(load_host_detail(self.old.id, timezone.now(), limit=2))
        self.assertEqual(
            (len(detail.contributions), detail.workloads_total, detail.truncated),
            (2, 3, True),
        )
        self.assertFalse(detail.host.online)

    def test_unknown_machine_redirects(self):
        self.login()
        self.assertRedirects(
            self.client.get('/machines/999999/'),
            '/machines/',
            fetch_redirect_response=False,
        )

    def test_users_page(self):
        self.login()
        Profile.objects.filter(user=self.worker).update(games=500)
        Profile.objects.filter(user=self.reader).update(tests=2)
        create_user('idle')

        rows = {item.username: item for item in self.client.get('/users/').context['rows']}
        self.assertEqual(set(rows), {'lab-worker', 'reader'})
        self.assertEqual(
            (rows['lab-worker'].machines_online, rows['lab-worker'].threads_online),
            (1, 8),
        )
        self.assertEqual((rows['reader'].machines_online, rows['reader'].threads_online), (0, 0))
        self.assertLess(timezone.now() - rows['lab-worker'].last_activity, timedelta(minutes=1))
        self.assertLess(timezone.now() - rows['reader'].last_activity, timedelta(minutes=1))

    def test_last_activity_ignores_logins(self):
        idle = create_user('idle', approver=True)
        self.client.force_login(idle)
        rows = {item.username: item for item in load_user_rows(timezone.now())}
        self.assertIsNone(rows['idle'].last_activity)

    def test_user_with_only_an_online_machine_is_listed(self):
        rows = {item.username: item for item in load_user_rows(timezone.now())}
        self.assertIn('lab-worker', rows)
        self.assertEqual(rows['lab-worker'].games, 0)

    def test_users_query_count_is_bounded(self):
        self.login()
        for index in range(6):
            owner = create_user(f'user-{index}')
            Profile.objects.filter(user=owner).update(games=index + 1)
            make_machine(owner, f'box-{index}')

        with self.assertNumQueries(8):
            self.client.get('/users/')

        for index in range(6, 12):
            owner = create_user(f'user-{index}')
            Profile.objects.filter(user=owner).update(games=index + 1)
            make_machine(owner, f'box-{index}', seen=timedelta(hours=1))

        self.assertEqual(query_count(self.client, '/users/'), 8)


class HostGroupingTests(TestCase):
    def setUp(self):
        ensure_book()
        self.reader = create_user('reader')
        self.worker = create_user('lab-worker')
        self.first, self.second = create_test(self.reader), create_test(self.reader)

        self.earliest = self.session(timedelta(days=9), self.first.id)
        self.earlier = self.session(timedelta(hours=3), self.first.id)
        self.overlapping = self.session(timedelta(seconds=50), self.second.id, concurrency=6)
        self.current = self.session(timedelta(seconds=5), 0, concurrency=8)
        add_result(self.first, self.earliest, (1, 2, 3, 2, 1))
        add_result(self.first, self.earlier, PENTA)
        add_result(self.second, self.overlapping, (0, 1, 1, 1, 0))
        self.other = make_machine(self.worker, 'other-box', cpu='Apple M4', concurrency=2, workload=self.second.id)

    def session(self, seen: timedelta, workload: int, concurrency: int = 4) -> Machine:
        return make_machine(self.worker, concurrency=concurrency, seen=seen, workload=workload, cli_options=SUPERVISED)

    def test_registrations_share_the_stored_host_key(self):
        keys = set(Machine.objects.filter(info__machine_name='box').values_list('host_key', flat=True))
        self.assertEqual(keys, {host_key('lab-worker', self.current.info)})
        self.assertNotEqual(self.other.host_key, self.current.host_key)

    def test_migration_keys_existing_registrations(self):
        keys = dict(Machine.objects.values_list('id', 'host_key'))
        Machine.objects.update(host_key='')

        migration = import_module('OpenBench.migrations.0019_machine_host_key')
        migration.fill_host_keys(apps, None)

        self.assertEqual(dict(Machine.objects.values_list('id', 'host_key')), keys)

    def test_migration_froze_the_current_key_rule(self):
        frozen = import_module('OpenBench.migrations.0019_machine_host_key').host_key
        samples: tuple[object, ...] = (
            self.current.info,
            self.other.info,
            {**self.current.info, 'mac_address': 'A5B1C2D3E4F5', 'machine_name': 'None'},
            {'mac_address': '0xA4', 'logical_cores': 'many', 'cpu_name': 7},
            None,
        )
        for info in samples:
            self.assertEqual(frozen('lab-worker', info), host_key('lab-worker', info))

    def test_a_host_is_one_row_with_its_registrations_summed(self):
        page = load_machines_page(timezone.now(), OfflineWindow.NONE)
        host = next(item for item in host_rows(page) if item.name == 'box')

        self.assertEqual(len(page.rows), 2)
        self.assertEqual((host.machine_id, host.threads, host.sessions), (self.current.id, 8, 4))
        self.assertEqual(host.lifetime_games, 2 * (9 + sum(PENTA) + 3))
        self.assertLess(abs(host.first_seen - (timezone.now() - timedelta(days=9))), timedelta(minutes=1))
        self.assertIsNone(host.workload)
        self.assertEqual((page.summary.online, page.summary.offline, page.summary.threads), (2, 0, 10))
        self.assertEqual(
            {group.cpu_name: (group.online, group.hosts, group.threads) for group in page.cpus},
            {'Ryzen 9': (1, 1, 8), 'Apple M4': (1, 1, 2)},
        )

    def test_an_offline_host_is_listed_once_in_a_window(self):
        Machine.objects.filter(id__in=[self.current.id, self.overlapping.id]).update(
            updated=timezone.now() - timedelta(hours=1)
        )
        page = load_machines_page(timezone.now(), OfflineWindow.DAY)
        self.assertEqual(
            [(item.name, item.online, item.sessions) for item in page.rows], [('other-box', True, 1), ('box', False, 4)]
        )
        self.assertEqual((page.summary.online, page.summary.offline), (1, 1))

    def test_page_rolls_offline_jobs_into_a_pool_and_expands_it(self):
        for index, job in enumerate(JOB_IDS[:3]):
            machine = make_machine(self.worker, f'batch-{job}:0', cpu='AMD EPYC 9R14', seen=timedelta(hours=index + 1))
            add_result(self.first, machine, (0, 1, 1, 1, 0))
            make_registration(self.worker, machine, 60 * (index + 1) + 5)
        self.client.force_login(self.reader)

        response = self.client.get('/machines/?show=24h')
        page = response.context['page']
        pool = next(item for item in page.rows if item.is_pool)
        self.assertEqual([item.name for item in page.rows], ['other-box', 'box', 'batch-*'])
        self.assertEqual((pool.hosts, pool.sessions, pool.lifetime_games), (3, 6, 18))
        self.assertEqual((page.summary.offline, page.offline_listed, page.truncated), (3, 3, False))
        self.assertContains(response, f'href="?show=24h&amp;pool={pool.key}"')
        self.assertContains(response, '3 machines')
        self.assertIsNone(page.expanded)

        expanded = self.client.get(f'/machines/?show=24h&pool={pool.key}')
        rows = expanded.context['page'].rows
        self.assertEqual([item.label for item in rows[2:]], [f'batch-{job[:8]}…:0' for job in JOB_IDS[:3]])
        self.assertEqual(expanded.context['page'].expanded, Pool('lab-worker', 'batch-*', 'AMD EPYC 9R14'))
        self.assertContains(expanded, 'Group them again')
        self.assertContains(expanded, f'title="batch-{JOB_IDS[0]}:0"')

        host_page = self.client.get(f'/machines/{self.earlier.id}/')
        for page_response in (response, expanded, host_page):
            audit = row_audit(page_response.content.decode())
            self.assertEqual(audit.defects(), [])
            self.assertGreaterEqual(len(audit.rows), 3)

        ignored = self.client.get('/machines/?show=24h&pool=not-a-key').context['page']
        self.assertEqual(len(ignored.rows), 3)
        self.assertEqual(
            query_count(self.client, f'/machines/?show=24h&pool={pool.key}'),
            query_count(self.client, '/machines/?show=24h'),
        )

    def test_tied_heartbeats_still_give_one_current_session(self):
        moment = timezone.now()
        Machine.objects.filter(info__machine_name='box').update(updated=moment)
        current = current_sessions(Machine.objects.all())
        self.assertEqual(set(current.values_list('id', flat=True)), {self.current.id, self.other.id})

    def test_status_lines_count_overlapping_registrations_once(self):
        self.assertEqual(Machine.objects.filter(updated__gte=timezone.now() - timedelta(minutes=2)).count(), 3)
        self.assertEqual(getMachineStatus(), ': 2 Machines / 10 Threads / 15.0 MNPS ')
        self.assertEqual(getMachineStatus('lab-worker'), ': 2 Machines / 10 Threads / 15.0 MNPS ')
        self.assertEqual(getMachineStatus('reader'), ': 0 Machines / 0 Threads / 0 MNPS ')

        fleet = load_fleet(timezone.now())
        self.assertEqual((fleet.machines, fleet.threads), (2, 10))

    def test_users_page_counts_hosts(self):
        rows = {item.username: item for item in load_user_rows(timezone.now())}
        self.assertEqual((rows['lab-worker'].machines_online, rows['lab-worker'].threads_online), (2, 10))

    def test_any_registration_resolves_to_the_host(self):
        self.client.force_login(self.reader)
        response = self.client.get(f'/machines/{self.earlier.id}/')
        detail = response.context['detail']

        self.assertEqual((detail.selected.id, detail.current.id), (self.earlier.id, self.current.id))
        self.assertEqual((detail.host.threads, detail.host.online, detail.sessions_total), (8, True, 4))
        self.assertEqual(detail.host.lifetime_games, 2 * (9 + sum(PENTA) + 3))
        self.assertEqual(
            [(item.machine_id, item.selected, item.games) for item in detail.sessions],
            [
                (self.current.id, False, 0),
                (self.overlapping.id, False, 6),
                (self.earlier.id, True, 2 * sum(PENTA)),
                (self.earliest.id, False, 18),
            ],
        )
        self.assertEqual(detail.sessions[2].workload, self.first)
        self.assertContains(response, 'class="fleet-selected" aria-current="true"', count=1)

    def test_workloads_pool_the_counters_of_every_registration(self):
        detail = present(load_host_detail(self.current.id, timezone.now()))
        pooled = tuple(a + b for a, b in zip(PENTA, (1, 2, 3, 2, 1), strict=True))
        merged = next(item for item in detail.contributions if item.test.id == self.first.id)

        self.assertEqual(detail.workloads_total, 2)
        self.assertEqual((merged.games, merged.sessions, merged.nps), (2 * sum(pooled), 2, 1_500_000))
        self.assertAlmostEqual(present(merged.elo).value, Elo(pooled)[1])
        self.assertAlmostEqual(present(merged.elo).lower, Elo(pooled)[0])

    def test_the_selected_session_is_listed_beyond_the_limit(self):
        detail = present(load_host_detail(self.earliest.id, timezone.now(), session_limit=2))
        self.assertEqual([item.machine_id for item in detail.sessions], [self.current.id, self.earliest.id])
        self.assertTrue(detail.sessions_truncated)

    def test_query_counts_do_not_grow_with_registrations(self):
        self.client.force_login(self.reader)
        urls = ['/machines/?show=7d', f'/machines/{self.earlier.id}/', '/users/', '/index/']
        before = [query_count(self.client, url) for url in urls]
        self.assertEqual(before, [9, 11, 8, 14])

        for index in range(12):
            extra = self.session(timedelta(minutes=index), create_test(self.reader).id)
            add_result(Test.objects.get(id=extra.workload), extra)

        self.assertEqual([query_count(self.client, url) for url in urls], before)


class HousekeepingTests(TestCase):
    def setUp(self):
        ensure_book()
        create_engine_config()
        self.worker, self.other = create_user('lab-worker'), create_user('home-worker')
        self.test = create_test(create_user('admin', approver=True), finished=True)

    def idle(self, minutes: float, owner: User | None = None, options: str = SUPERVISED, name: str = 'box') -> Machine:
        return make_machine(owner or self.worker, name, seen=timedelta(minutes=minutes), cli_options=options)

    def register(self, **info: object) -> Machine:
        payload = register_payload(
            self.worker, machine_name='box', mac_address=mac_of('box'), cli_options=SUPERVISED, **info
        )
        response = self.client.post('/clientWorkerInfo/', payload).json()
        return Machine.objects.get(id=response['machine_id'])

    def test_registering_removes_the_owners_exited_idle_sessions(self):
        registered = self.register()
        key = registered.host_key
        stale = [make_registration(self.worker, registered, minutes) for minutes in (16, 60, 3000)]
        fresh = make_registration(self.worker, registered, 14)
        used = make_registration(self.worker, registered, 500)
        add_result(self.test, used)
        polling = make_registration(self.worker, registered, 500, cli_options='--threads 4')
        elsewhere = self.idle(500, name='other-box')
        foreign = self.idle(500, owner=self.other)

        newest = self.register()

        self.assertEqual(newest.host_key, key)
        self.assertFalse(Machine.objects.filter(id__in=[machine.id for machine in stale]).exists())
        self.assertFalse(Machine.objects.filter(id=elsewhere.id).exists())
        kept = {registered.id, fresh.id, used.id, polling.id, foreign.id, newest.id}
        self.assertEqual(set(Machine.objects.values_list('id', flat=True)), kept)

    def test_an_idle_supervised_host_keeps_a_bounded_number_of_rows(self):
        for _ in range(100):
            self.register()
            Machine.objects.update(updated=F('updated') - timedelta(minutes=1))

        self.assertLessEqual(Machine.objects.count(), EXITED_AFTER.seconds // 60 + 1)
        self.assertGreaterEqual(Machine.objects.count(), 10)

    def test_a_backlog_is_cleared_a_bounded_batch_at_a_time(self):
        registered = self.register()
        for _ in range(8):
            make_registration(self.worker, registered, 60)

        self.assertEqual(prune_exited_sessions(self.worker.id, timezone.now(), window=3), 3)
        self.assertEqual(Machine.objects.count(), 6)

    def test_ephemeral_hosts_that_never_return_are_cleared_by_any_registration(self):
        used = []
        for job in range(20):
            name = f'batch-{uuid.UUID(int=job + 1)}:0'
            for minute in range(20):
                self.idle(16 + job * 30 + minute, name=name)
            used.append(self.idle(16 + job * 30, name=name))
            add_result(self.test, used[-1])
        bystander = self.idle(600, owner=self.other, name='batch-elsewhere')
        self.assertEqual(Machine.objects.count(), 421)

        newest = self.register()

        self.assertEqual(
            set(Machine.objects.values_list('id', flat=True)), {newest.id, bystander.id, *(row.id for row in used)}
        )

    def test_registration_queries_do_not_grow_with_the_backlog(self):
        registered = self.register()
        counts = []
        for backlog in (2, 30):
            for _ in range(backlog):
                make_registration(self.worker, registered, 60)
            with CaptureQueriesContext(connection) as context:
                self.register()
            counts.append(len(context.captured_queries))
        self.assertEqual(counts[0], counts[1])
        self.assertEqual(Machine.objects.count(), 3)

    def test_a_polling_clients_removed_session_registers_again(self):
        payload = {'machine_id': 999999, 'secret': 'gone'}
        self.assertEqual(
            self.client.post('/clientGetWorkload/', payload).json(), {'error': 'Bad Client Version: Bad Machine Id'}
        )

    def test_an_idle_poll_does_not_refresh_the_heartbeat(self):
        registered = self.register()
        Machine.objects.filter(id=registered.id).update(updated=timezone.now() - timedelta(hours=1))
        session = {'machine_id': registered.id, 'secret': registered.secret}

        self.assertEqual(self.client.post('/clientGetWorkload/', session).json(), {})
        self.assertLess(Machine.objects.get(id=registered.id).updated, timezone.now() - timedelta(minutes=59))

    def test_plan_separates_exited_from_abandoned_and_keeps_the_rest(self):
        exited = self.idle(20)
        fleet = self.idle(20, options='--fleet --threads 4', name='fleet-box')
        abandoned = self.idle(60 * 24 * 8, options='--threads 4', name='polling-box')
        self.idle(60 * 24 * 6, options='--threads 4', name='recent-polling-box')
        self.idle(5)
        used = self.idle(60 * 24 * 30)
        add_result(self.test, used)

        default = plan_prune(timezone.now())
        self.assertEqual((default.exited, default.abandoned, default.unkeyed), ([exited.id, fleet.id], [], 0))

        plan = plan_prune(timezone.now(), timedelta(days=7))
        self.assertEqual((plan.registrations, plan.in_use), (6, 1))
        self.assertEqual((plan.exited, plan.abandoned), ([exited.id, fleet.id], [abandoned.id]))
        self.assertEqual(never_used(Machine.objects.all()).count(), 5)

        self.assertEqual(apply_prune(plan), 3)
        self.assertEqual(Machine.objects.count(), 3)
        self.assertTrue(Machine.objects.filter(id=used.id).exists())

    def test_a_registration_that_gains_a_result_after_planning_is_kept(self):
        exited = self.idle(20)
        plan = plan_prune(timezone.now())
        add_result(self.test, exited)

        self.assertEqual(apply_prune(plan), 0)
        self.assertTrue(Machine.objects.filter(id=exited.id).exists())

    def test_command_reports_by_default_and_deletes_on_request(self):
        self.idle(20)
        self.idle(60 * 24 * 3, options='--threads 4', name='polling-box')

        out = io.StringIO()
        call_command('prune_machines', stdout=out)
        self.assertIn('2 registrations of 2 machines; 0 have Results', out.getvalue())
        self.assertIn('Dry run: 1 registrations would be deleted', out.getvalue())
        self.assertEqual(Machine.objects.count(), 2)

        call_command('prune_machines', '--dry-run', '--include-polling', '--days', '2', stdout=out)
        self.assertIn('Dry run: 2 registrations would be deleted', out.getvalue())
        self.assertEqual(Machine.objects.count(), 2)

        call_command('prune_machines', '--apply', '--days', '2', stdout=out)
        self.assertIn('Deleted 1 of 1 registrations', out.getvalue())
        self.assertEqual([machine.info['machine_name'] for machine in Machine.objects.all()], ['polling-box'])

        call_command('prune_machines', '--apply', '--include-polling', '--days', '2', stdout=out)
        self.assertEqual(Machine.objects.count(), 0)

    def test_command_rejects_contradictory_or_unsafe_options(self):
        for arguments in (('--apply', '--dry-run'), ('--days', '0')):
            with self.assertRaises(CommandError):
                call_command('prune_machines', *arguments, stdout=io.StringIO())


def insert_as_old_code(owner: User, info: dict[str, object], seen: timedelta = timedelta()) -> Machine:
    # What a rolled-back image does: it has no host_key field, so its INSERT never names the column
    with connection.cursor() as cursor:
        cursor.execute(
            'INSERT INTO "OpenBench_machine" (user_id, mnps, dev_mnps, base_mnps, updated, secret, info, workload) '
            'VALUES (%s, 1.5, 1.5, 1.5, %s, %s, %s, 0)',
            [owner.id, timezone.now() - seen, 'old-secret', json.dumps(info)],
        )
        return Machine.objects.get(id=cursor.lastrowid)


class RollbackSafetyTests(TestCase):
    def setUp(self):
        ensure_book()
        create_engine_config()
        self.worker = create_user('lab-worker')
        self.reader = create_user('reader')
        self.test = create_test(create_user('admin', approver=True))
        self.info = {
            **system_info(concurrency=8),
            'machine_name': 'box',
            'mac_address': mac_of('box'),
            'cli_options': '',
        }
        self.other_info = {**self.info, 'machine_name': 'other', 'mac_address': mac_of('other')}

    def test_old_code_can_still_register_and_its_rows_are_unkeyed(self):
        self.assertEqual(insert_as_old_code(self.worker, self.info).host_key, '')

    def test_unkeyed_registrations_are_never_merged_with_each_other(self):
        insert_as_old_code(self.worker, self.info)
        insert_as_old_code(self.worker, self.other_info)

        self.assertEqual(getMachineStatus(), ': 2 Machines / 16 Threads / 24.0 MNPS ')
        self.assertEqual(load_fleet(timezone.now()).machines, 2)
        rows = {item.username: item for item in load_user_rows(timezone.now())}
        self.assertEqual(rows['lab-worker'].machines_online, 2)

    def test_pages_and_insights_key_unkeyed_rows_from_their_info(self):
        first = insert_as_old_code(self.worker, self.info)
        again = insert_as_old_code(self.worker, self.info, seen=timedelta(hours=1))
        other = insert_as_old_code(self.worker, self.other_info)
        for machine in (first, again, other):
            add_result(self.test, machine, (0, 1, 1, 1, 0))
        self.client.force_login(self.reader)

        machines = result_rows(self.test)
        self.assertEqual(len({item.host for item in machines}), 2)
        self.assertNotIn('', {item.host for item in machines})

        page = self.client.get('/machines/').context['page']
        self.assertEqual(
            {item.name: (item.sessions, item.lifetime_games) for item in page.rows}, {'box': (2, 12), 'other': (1, 6)}
        )

        Machine.objects.update(host_key='')
        detail = self.client.get(f'/machines/{again.id}/').context['detail']
        self.assertEqual((detail.sessions_total, detail.host.lifetime_games), (2, 12))

    def test_a_heartbeat_keys_its_own_row(self):
        machine = insert_as_old_code(self.worker, self.info)
        machine.save(update_fields=['updated'])
        self.assertEqual(Machine.objects.get(id=machine.id).host_key, host_key('lab-worker', self.info))

    def test_registration_keys_rows_left_by_old_code(self):
        old = [insert_as_old_code(self.worker, self.info), insert_as_old_code(self.reader, self.other_info)]
        self.client.post('/clientWorkerInfo/', register_payload(self.worker))
        self.assertEqual(
            [Machine.objects.get(id=machine.id).host_key for machine in old],
            [host_key('lab-worker', self.info), host_key('reader', self.other_info)],
        )

    def test_rekeying_is_bounded_per_call(self):
        for _ in range(5):
            insert_as_old_code(self.worker, self.info)
        self.assertEqual(rekey_unkeyed(limit=3), 3)
        self.assertEqual(Machine.objects.filter(host_key='').count(), 2)

    def test_prune_machines_reports_and_keys_unkeyed_rows(self):
        insert_as_old_code(self.worker, self.info)
        out = io.StringIO()
        call_command('prune_machines', stdout=out)
        self.assertIn('1 registrations have no host key', out.getvalue())
        self.assertEqual(Machine.objects.filter(host_key='').count(), 1)

        call_command('prune_machines', '--apply', stdout=out)
        self.assertIn('Keyed 1 registrations', out.getvalue())
        self.assertFalse(Machine.objects.filter(host_key='').exists())

    def test_failed_housekeeping_never_fails_a_registration(self):
        for target in ('prune_exited_sessions', 'rekey_unkeyed'):
            with (
                mock.patch(f'OpenBench.fleet.housekeeping.{target}', side_effect=DatabaseError('disk I/O error')),
                self.assertLogs('OpenBench.fleet.housekeeping', level='ERROR'),
            ):
                response = self.client.post('/clientWorkerInfo/', register_payload(self.worker)).json()
            self.assertTrue(Machine.objects.filter(id=response['machine_id'], secret=response['secret']).exists())


class ConcurrentSlotTests(TestCase):
    def setUp(self):
        self.worker = create_user('lab-worker')
        job = f'batch-{JOB_IDS[0]}'
        self.slots = [self.slot(f'{job}:{index}') for index in (0, 1)]

    def slot(self, name: str, seen: timedelta = timedelta(), mac: str = '0242AC110002') -> Machine:
        info = {**system_info(concurrency=8), 'cpu_name': 'AMD EPYC 9R14', 'machine_name': name, 'mac_address': mac}
        machine = Machine.objects.create(user=self.worker, info=info, mnps=1.0)
        Machine.objects.filter(id=machine.id).update(updated=timezone.now() - seen)
        return machine

    def test_slots_of_one_vm_are_counted_as_two_machines(self):
        self.assertEqual(getMachineStatus(), ': 2 Machines / 16 Threads / 16.0 MNPS ')
        fleet = load_fleet(timezone.now())
        self.assertEqual((fleet.machines, fleet.threads), (2, 16))
        rows = {item.username: item for item in load_user_rows(timezone.now())}
        self.assertEqual((rows['lab-worker'].machines_online, rows['lab-worker'].threads_online), (2, 16))
        page = load_machines_page(timezone.now(), OfflineWindow.NONE)
        self.assertEqual((page.summary.online, page.summary.threads, len(page.rows)), (2, 16, 2))

    def test_a_restarted_slot_is_still_counted_once(self):
        for mac in ('86A1B2C3D4E5', '3DA1B2C3D4E5'):
            self.slot(f'batch-{JOB_IDS[0]}:0', seen=timedelta(seconds=30), mac=mac)
        self.assertEqual(getMachineStatus(), ': 2 Machines / 16 Threads / 16.0 MNPS ')

    def test_jobs_behind_the_same_container_address_stay_apart_and_share_a_pool(self):
        other = self.slot(f'batch-{JOB_IDS[1]}:0')
        hosts = {machine.host_key for machine in (*self.slots, other)}
        self.assertEqual(len(hosts), 3)

        Machine.objects.update(updated=timezone.now() - timedelta(hours=1))
        page = load_machines_page(timezone.now(), OfflineWindow.DAY)
        self.assertEqual([(item.name, item.hosts) for item in page.rows], [('batch-*', 3)])
