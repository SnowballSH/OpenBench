# Populate an empty development database with a representative Server:
#
# >>> OPENBENCH_DEBUG=1 python3 manage.py seed_demo
#
# Creates accounts, an Engine, a Book, a fleet of Machines, and Workloads in
# every state, each with per-Machine Results. Refuses to run without DEBUG, or
# against a database that already holds Workloads.

import datetime
import random
from dataclasses import dataclass

from django.conf import settings
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from OpenBench.config import OPENBENCH_CONFIG
from OpenBench.models import Book, Engine, EngineConfig, Machine, Profile, Result, Test
from OpenBench.stats import PentanomialSPRT

DEMO_PASSWORD = 'openbench-demo'

ENGINE_SOURCE = 'https://github.com/SnowballSH/Avalanche'

CPUS = [
    ('AMD Ryzen 9 7950X 16-Core Processor', 'x86-64-avx512', 'Linux', 32),
    ('AMD Ryzen 9 7950X 16-Core Processor', 'x86-64-avx512', 'Linux', 32),
    ('Intel(R) Core(TM) i9-13900K', 'x86-64-avx2', 'Linux', 24),
    ('Apple M4', 'apple-silicon', 'Darwin', 10),
    ('AMD EPYC 7763 64-Core Processor', 'x86-64-avx2', 'Linux', 64),
]

@dataclass(frozen=True)
class DemoWorkload:
    name       : str
    mode       : str
    elo        : float
    pairs      : int
    state      : str
    priority   : int = 0
    max_games  : int = 0
    bounds     : tuple = (0.0, 3.0)
    tc         : str = '8.0+0.08'
    threads    : int = 1

WORKLOADS = [
    DemoWorkload('lmr-tweak',        'SPRT',  2.5, 5200, 'active', priority=1),
    DemoWorkload('history-bonus',    'SPRT', -1.0, 2600, 'active'),
    DemoWorkload('nezha-v2',         'SPRT',  6.0, 3100, 'passed', bounds=(0.0, 5.0)),
    DemoWorkload('aspiration-width', 'SPRT', -3.0, 1800, 'failed'),
    DemoWorkload('smp-scaling',      'GAMES', 12.0, 400, 'active', max_games=4000, tc='20.0+0.2', threads=4),
    DemoWorkload('qsearch-see',      'SPRT',  1.0,    0, 'pending'),
    DemoWorkload('ltc-regression',   'GAMES', -0.5, 1000, 'passed', max_games=2000, tc='40.0+0.4'),
]

class Command(BaseCommand):

    help = 'Fill an empty DEBUG database with demonstration users, machines and workloads'

    def add_arguments(self, parser):
        parser.add_argument('--seed', type=int, default=20260929, help='Seed for the pseudo-random results')

    def handle(self, *args, **options):

        if not settings.DEBUG:
            raise CommandError('seed_demo only runs with OPENBENCH_DEBUG enabled')

        if Test.objects.exists():
            raise CommandError('The database already holds Workloads; seed_demo only fills an empty one')

        rng = random.Random(options['seed'])

        with transaction.atomic():
            users    = create_users()
            create_engine_config()
            create_book()
            machines = create_machines(users)
            for spec in WORKLOADS:
                create_workload(spec, users[0], machines, rng)

        self.stdout.write('Seeded %d workloads on %d machines. Log in as admin / %s' % (
            len(WORKLOADS), len(machines), DEMO_PASSWORD))

def create_users():
    specs = [('admin', True), ('lab-worker', False), ('home-worker', False)]
    users = []
    for username, is_admin in specs:
        user = User.objects.create_user(username, '', DEMO_PASSWORD, is_staff=is_admin, is_superuser=is_admin)
        Profile.objects.create(user=user, enabled=True, approver=is_admin, superuser=is_admin)
        users.append(user)
    return users

def create_engine_config():
    presets = { 'test_presets' : { 'default' : {} }, 'tune_presets' : { 'default' : {} }, 'datagen_presets' : { 'default' : {} } }
    EngineConfig.objects.get_or_create(name='Avalanche', defaults={
        'nps' : 1500000, 'source' : ENGINE_SOURCE, 'build_path' : '', 'build_compilers' : 'zig>=0.16.0',
        'build_cpuflags' : '', 'build_systems' : 'Linux Darwin', 'presets' : presets })

def create_book():
    Book.objects.get_or_create(name='UHO_Lichess_4852_v1.epd', defaults={
        'source' : 'https://example.invalid/book.zip', 'sha' : '0' * 64 })

def create_machines(users):
    machines = []
    for index, (cpu_name, isa_name, os_name, threads) in enumerate(CPUS):
        owner = users[1 + index % 2]
        info  = {
            'cpu_name' : cpu_name, 'isa_name' : isa_name, 'os_name' : os_name, 'os_ver' : '',
            'concurrency' : threads, 'logical_cores' : threads, 'physical_cores' : threads // 2,
            'cpu_flags' : [], 'compilers' : { 'Avalanche' : ['zig', '0.16.0'] }, 'tokens' : {},
            'ram_total_mb' : 65536, 'syzygy_max' : 0, 'noisy' : False, 'sockets' : 1,
            'machine_name' : 'demo-%d' % (index + 1), 'client_ver' : OPENBENCH_CONFIG['client_version'],
            'supported' : ['Avalanche'],
        }
        mnps = round(1.2 + 0.4 * index, 2)
        machines.append(Machine.objects.create(user=owner, info=info, mnps=mnps, dev_mnps=mnps, base_mnps=mnps))
    return machines

def simulate_pairs(elo, pairs, rng):
    score    = 1 / (1 + 10 ** (-elo / 400))
    draw     = 0.55
    win_pair = (1 - draw) * score
    penta    = [0, 0, 0, 0, 0]
    for _ in range(pairs):
        first  = 1.0 if rng.random() < win_pair else (0.5 if rng.random() < draw / (1 - win_pair) else 0.0)
        second = 1.0 if rng.random() < win_pair else (0.5 if rng.random() < draw / (1 - win_pair) else 0.0)
        penta[int(2 * (first + second))] += 1
    return penta

def split_pairs(penta, parts, rng):
    weights = [rng.uniform(0.5, 2.0) for _ in range(parts)]
    shares  = [[0] * 5 for _ in range(parts)]
    for bucket, count in enumerate(penta):
        for _ in range(count):
            shares[rng.choices(range(parts), weights)[0]][bucket] += 1
    return shares

def create_workload(spec, author, machines, rng):

    dev  = Engine.objects.create(name=spec.name, source=ENGINE_SOURCE, sha='%040x' % rng.getrandbits(160), bench=rng.randint(2_000_000, 4_000_000))
    base = Engine.objects.create(name='master', source=ENGINE_SOURCE, sha='%040x' % rng.getrandbits(160), bench=rng.randint(2_000_000, 4_000_000))
    options = 'Threads=%d Hash=%d' % (spec.threads, 16 * spec.threads)

    created = timezone.now() - datetime.timedelta(hours=rng.uniform(1, 72))
    test = Test.objects.create(
        author=author.username, book_name='UHO_Lichess_4852_v1.epd',
        dev=dev, dev_repo=ENGINE_SOURCE, dev_engine='Avalanche', dev_options=options,
        dev_network='BCF481FD', dev_netname='nezha', dev_time_control=spec.tc,
        base=base, base_repo=ENGINE_SOURCE, base_engine='Avalanche', base_options=options,
        base_network='BCF481FD', base_netname='nezha', base_time_control=spec.tc,
        test_mode=spec.mode, max_games=spec.max_games, priority=spec.priority, throughput=1000,
        elolower=spec.bounds[0], eloupper=spec.bounds[1], alpha=0.05, beta=0.05, lowerllr=-2.94, upperllr=2.94,
        approved=spec.state != 'pending', info='Seeded demonstration workload',
    )

    penta = simulate_pairs(spec.elo, spec.pairs, rng)
    for machine, share in zip(machines, split_pairs(penta, len(machines), rng)):
        if sum(share):
            create_result(test, machine, share, rng)

    wins, losses, draws = trinomial(penta)
    finished = spec.state in ('passed', 'failed')
    Test.objects.filter(id=test.id).update(
        wins=wins, losses=losses, draws=draws, LL=penta[0], LD=penta[1], DD=penta[2], DW=penta[3], WW=penta[4], games=2 * sum(penta),
        currentllr=PentanomialSPRT(penta, spec.bounds[0], spec.bounds[1]) if spec.mode == 'SPRT' and sum(penta) else 0.0,
        passed=spec.state == 'passed', failed=spec.state == 'failed', finished=finished,
        creation=created,
    )

    if spec.state == 'active':
        for machine in machines[:3]:
            Machine.objects.filter(id=machine.id).update(workload=test.id, updated=timezone.now())

def trinomial(penta):
    wins   = 2 * penta[4] + penta[3]
    losses = 2 * penta[0] + penta[1]
    return wins, losses, 2 * sum(penta) - wins - losses

def create_result(test, machine, penta, rng):
    pairs = sum(penta)
    wins, losses, draws = trinomial(penta)
    time  = pairs * rng.randint(9_000, 11_000)
    nodes = int(time * machine.mnps * 1000)
    Result.objects.create(
        test=test, machine=machine, games=2 * pairs, wins=wins, losses=losses, draws=draws,
        LL=penta[0], LD=penta[1], DD=penta[2], DW=penta[3], WW=penta[4],
        dev_nodes=nodes, dev_time=time, dev_time_scaled=time,
        base_nodes=int(nodes * 0.98), base_time=time, base_time_scaled=time,
    )
