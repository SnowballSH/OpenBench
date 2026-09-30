# Populate an empty development database with a representative Server:
#
# >>> OPENBENCH_DEBUG=1 python3 manage.py seed_demo
#
# Creates accounts, an Engine, a Book, a fleet of Machines, and Workloads of
# every mode (SPRT, GAMES, SPSA, DATAGEN) in every state, each with per-Machine
# Results and a WorkloadSnapshot history. Refuses to run without DEBUG, or
# against a database that already holds Workloads.

import datetime
import random
from dataclasses import dataclass

from django.conf import settings
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from OpenBench.config import OPENBENCH_CONFIG
from OpenBench.models import Book, Engine, EngineConfig, Machine, Profile, Result, SPSAParameter, SPSARun, Test, WorkloadSnapshot
from OpenBench.stats import PentanomialSPRT

DEMO_PASSWORD = 'openbench-demo'

HISTORY_POINTS = 150

ENGINE_SOURCE = 'https://github.com/SnowballSH/Avalanche'

BOOK_NAME = 'UHO_Lichess_4852_v1.epd'

# The last field is how many hours ago an offline Machine last reported; 0 is online
CPUS = [
    ('AMD Ryzen 9 7950X 16-Core Processor', 'x86-64-avx512', 'Linux', 32, 0),
    ('AMD Ryzen 9 7950X 16-Core Processor', 'x86-64-avx512', 'Linux', 32, 0),
    ('Intel(R) Core(TM) i9-13900K', 'x86-64-avx2', 'Linux', 24, 0),
    ('Apple M4', 'apple-silicon', 'Darwin', 10, 5),
    ('AMD EPYC 7763 64-Core Processor', 'x86-64-avx2', 'Linux', 64, 30),
    ('Intel(R) Xeon(R) Gold 6338 CPU @ 2.00GHz', 'x86-64-avx512', 'Linux', 32, 240),
]

FINISHED_STATES = ('passed', 'failed', 'finished')

@dataclass(frozen=True)
class DemoWorkload:
    name          : str
    mode          : str
    elo           : float
    pairs         : int
    state         : str
    priority      : int = 0
    max_games     : int = 0
    bounds        : tuple = (0.0, 3.0)
    tc            : str = '8.0+0.08'
    threads       : int = 1
    book          : str = BOOK_NAME
    upload_pgns   : str = 'FALSE'
    genfens_args  : str = ''
    play_reverses : bool = False

@dataclass(frozen=True)
class DemoParameter:
    name      : str
    is_float  : bool
    start     : float
    min_value : float
    max_value : float
    c_end     : float
    r_end     : float
    optimum   : float

@dataclass(frozen=True)
class DemoTune:
    name         : str
    state        : str
    iterations   : int
    played       : int
    parameters   : tuple[DemoParameter, ...]
    pairs_per    : int = 8
    alpha        : float = 0.602
    gamma        : float = 0.101
    a_ratio      : float = 0.1
    reporting    : str = SPSARun.SPSAReportingType.BATCHED
    distribution : str = SPSARun.SPSADistributionType.SINGLE
    tc           : str = '8.0+0.08'
    priority     : int = 0

WORKLOADS = [
    DemoWorkload('lmr-tweak',        'SPRT',  2.5, 5200, 'active', priority=1),
    DemoWorkload('history-bonus',    'SPRT', -1.0, 2600, 'active'),
    DemoWorkload('nezha-v2',         'SPRT',  6.0, 3100, 'passed', bounds=(0.0, 5.0)),
    DemoWorkload('aspiration-width', 'SPRT', -3.0, 1800, 'failed'),
    DemoWorkload('smp-scaling',      'GAMES', 12.0, 400, 'active', max_games=4000, tc='20.0+0.2', threads=4),
    DemoWorkload('qsearch-see',      'SPRT',  1.0,    0, 'pending'),
    DemoWorkload('ltc-regression',   'GAMES', -0.5, 1000, 'passed', max_games=2000, tc='40.0+0.4'),
    DemoWorkload('nezha-v3-data',    'DATAGEN', 0.0, 2400, 'active', max_games=12000, tc='N=5000', book='NONE',
                 upload_pgns='COMPACT', genfens_args='-randmoves 8'),
    DemoWorkload('nezha-v2-data',    'DATAGEN', 0.0, 3000, 'passed', max_games=6000, tc='N=5000', book='NONE',
                 upload_pgns='COMPACT', play_reverses=True),
]

SEARCH_PARAMETERS = (
    DemoParameter('LmrBase',          True,     0.75,    0.25,     1.50,   0.08, 0.002,     0.92),
    DemoParameter('LmrDivisor',       True,     2.25,    1.50,     3.50,   0.15, 0.002,     2.05),
    DemoParameter('RfpMargin',        False,   75.0,    30.0,    150.0,    8.0,  0.002,    64.0),
    DemoParameter('NmpBaseReduction', False,    3.0,     1.0,      6.0,    0.5,  0.002,     4.0),
    DemoParameter('AspirationWindow', False,   12.0,     5.0,     40.0,    3.0,  0.002,    16.0),
    DemoParameter('HistoryDivisor',   False, 8192.0,  2048.0,  16384.0,  600.0,  0.002, 10400.0),
)

EVAL_PARAMETERS = (
    DemoParameter('KnightMobility',   False,  4.0,  0.0, 16.0, 1.0,  0.002,  5.0),
    DemoParameter('BishopPairBonus',  False, 32.0,  0.0, 80.0, 4.0,  0.002, 41.0),
    DemoParameter('PassedPawnScale',  True,   1.10, 0.50, 2.00, 0.06, 0.002, 1.24),
    DemoParameter('KingSafetyWeight', True,   0.85, 0.40, 1.60, 0.05, 0.002, 0.97),
)

TUNES = [
    DemoTune('search-tune', 'active', iterations=2500, played=900, parameters=SEARCH_PARAMETERS, priority=1),
    DemoTune('eval-tune', 'finished', iterations=1200, played=1200, parameters=EVAL_PARAMETERS,
             distribution=SPSARun.SPSADistributionType.MULTIPLE),
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
            for tune in TUNES:
                create_tune(tune, users[0], machines, rng)
            credit_profiles(users)
            assign_online_machines(machines)
            age_offline_machines(machines)

        self.stdout.write('Seeded %d workloads on %d machines. Log in as admin / %s' % (
            len(WORKLOADS) + len(TUNES), len(machines), DEMO_PASSWORD))

def create_users():
    specs = [('admin', True), ('lab-worker', False), ('home-worker', False)]
    users = []
    for username, is_admin in specs:
        user = User.objects.create_user(username, '', DEMO_PASSWORD, is_staff=is_admin, is_superuser=is_admin)
        Profile.objects.create(user=user, enabled=True, approver=is_admin, superuser=is_admin)
        users.append(user)
    return users

def credit_profiles(users):
    for user in users:
        games = Result.objects.filter(machine__user=user).aggregate(total=Sum('games'))['total'] or 0
        tests = Test.objects.filter(author=user.username).count()
        Profile.objects.filter(user=user).update(games=games, tests=tests)

def create_engine_config():
    presets = { 'test_presets' : { 'default' : {} }, 'tune_presets' : { 'default' : {} }, 'datagen_presets' : { 'default' : {} } }
    EngineConfig.objects.get_or_create(name='Avalanche', defaults={
        'nps' : 1500000, 'source' : ENGINE_SOURCE, 'build_path' : '', 'build_compilers' : 'zig>=0.16.0',
        'build_cpuflags' : '', 'build_systems' : 'Linux Darwin', 'presets' : presets })

def create_book():
    Book.objects.get_or_create(name=BOOK_NAME, defaults={
        'source' : 'https://example.invalid/book.zip', 'sha' : '0' * 64 })

def create_machines(users):
    machines = []
    for index, (cpu_name, isa_name, os_name, threads, _) in enumerate(CPUS):
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

def assign_online_machines(machines: list[Machine]) -> None:
    active = list(Test.objects.filter(approved=True, finished=False).order_by('-priority', 'id').values_list('id', flat=True))
    online = [machine for machine, (*_, hours_ago) in zip(machines, CPUS) if not hours_ago]
    for index, machine in enumerate(online):
        Machine.objects.filter(id=machine.id).update(workload=active[index % len(active)], updated=timezone.now())

def age_offline_machines(machines):
    now = timezone.now()
    for machine, (*_, hours_ago) in zip(machines, CPUS):
        if not hours_ago:
            continue
        seen = now - datetime.timedelta(hours=hours_ago)
        Result.objects.filter(machine=machine, updated__gt=seen).update(updated=seen)
        last = Result.objects.filter(machine=machine).order_by('-updated', '-id').values_list('test_id', flat=True).first()
        Machine.objects.filter(id=machine.id).update(updated=seen, workload=last or 0)

def simulate_pairs(elo, pairs, rng):
    score    = 1 / (1 + 10 ** (-elo / 400))
    draw     = 0.55
    win_pair = (1 - draw) * score
    outcomes = []
    for _ in range(pairs):
        first  = 1.0 if rng.random() < win_pair else (0.5 if rng.random() < draw / (1 - win_pair) else 0.0)
        second = 1.0 if rng.random() < win_pair else (0.5 if rng.random() < draw / (1 - win_pair) else 0.0)
        outcomes.append(int(2 * (first + second)))
    return outcomes

def tally(outcomes):
    penta = [0, 0, 0, 0, 0]
    for bucket in outcomes:
        penta[bucket] += 1
    return penta

def split_pairs(penta, parts, rng):
    weights = [rng.uniform(0.5, 2.0) for _ in range(parts)]
    shares  = [[0] * 5 for _ in range(parts)]
    for bucket, count in enumerate(penta):
        for _ in range(count):
            shares[rng.choices(range(parts), weights)[0]][bucket] += 1
    return shares

@dataclass(frozen=True)
class Schedule:
    created : datetime.datetime
    started : datetime.datetime
    ended   : datetime.datetime

def schedule(state: str, rng: random.Random) -> Schedule:
    now     = timezone.now()
    created = now - datetime.timedelta(hours=rng.uniform(1, 72))
    started = created + datetime.timedelta(minutes=rng.uniform(2, 10))
    ended   = started + (now - started) * (rng.uniform(0.3, 0.9) if state in FINISHED_STATES else 1.0)
    return Schedule(created, started, ended)

def create_engine(name: str, rng: random.Random) -> Engine:
    sha = f'{rng.getrandbits(160):040x}'
    return Engine.objects.create(name=name, source=ENGINE_SOURCE, sha=sha, bench=rng.randint(2_000_000, 4_000_000))

def create_workload(spec, author, machines, rng):

    dev     = create_engine(spec.name, rng)
    base    = create_engine('master', rng)
    options = 'Threads=%d Hash=%d' % (spec.threads, 16 * spec.threads)
    times   = schedule(spec.state, rng)
    is_sprt = spec.mode == 'SPRT'
    is_data = spec.mode == 'DATAGEN'
    sprt    = { 'elolower' : spec.bounds[0], 'eloupper' : spec.bounds[1], 'alpha' : 0.05, 'beta' : 0.05, 'lowerllr' : -2.94, 'upperllr' : 2.94 }

    test = Test.objects.create(
        author=author.username, book_name=spec.book, upload_pgns=spec.upload_pgns,
        dev=dev, dev_repo=ENGINE_SOURCE, dev_engine='Avalanche', dev_options=options,
        dev_network='BCF481FD', dev_netname='nezha', dev_time_control=spec.tc,
        base=base, base_repo=ENGINE_SOURCE, base_engine='Avalanche', base_options=options,
        base_network='BCF481FD', base_netname='nezha', base_time_control=spec.tc,
        test_mode=spec.mode, max_games=spec.max_games, priority=spec.priority, throughput=1000,
        genfens_args=spec.genfens_args, play_reverses=spec.play_reverses,
        use_tri=is_data and not spec.play_reverses, use_penta=not is_data or spec.play_reverses,
        approved=spec.state != 'pending', info='Seeded demonstration workload',
        **(sprt if is_sprt else {}),
    )

    bounds = spec.bounds if is_sprt else None
    record_outcomes(test, simulate_pairs(spec.elo, spec.pairs, rng), spec.state, bounds, times, machines, rng)

def create_tune(spec: DemoTune, author: User, machines: list[Machine], rng: random.Random) -> None:

    engine  = create_engine(spec.name, rng)
    times   = schedule(spec.state, rng)
    options = 'Threads=1 Hash=16'

    test = Test.objects.create(
        author=author.username, book_name=BOOK_NAME,
        dev=engine, dev_repo=ENGINE_SOURCE, dev_engine='Avalanche', dev_options=options,
        dev_network='BCF481FD', dev_netname='nezha', dev_time_control=spec.tc,
        base=engine, base_repo=ENGINE_SOURCE, base_engine='Avalanche', base_options=options,
        base_network='BCF481FD', base_netname='nezha', base_time_control=spec.tc,
        test_mode='SPSA', workload_size=spec.pairs_per, priority=spec.priority, throughput=1000,
        approved=True, info='Seeded demonstration tune',
    )

    run = SPSARun.objects.create(
        tune=test, reporting_type=spec.reporting, distribution_type=spec.distribution,
        alpha=spec.alpha, gamma=spec.gamma, iterations=spec.iterations, pairs_per=spec.pairs_per, a_ratio=spec.a_ratio,
    )

    parameters = SPSAParameter.objects.bulk_create([
        SPSAParameter(
            spsa_run=run, name=param.name, index=index, value=param.start, is_float=param.is_float,
            start=param.start, min_value=param.min_value, max_value=param.max_value, c_end=param.c_end, r_end=param.r_end,
            c_value=param.c_end * spec.iterations ** spec.gamma,
            a_value=param.r_end * param.c_end ** 2 * (spec.a_ratio * spec.iterations + spec.iterations) ** spec.alpha,
        ) for index, param in enumerate(spec.parameters)
    ])

    outcomes = simulate_tune(spec, run, parameters, rng)
    SPSAParameter.objects.bulk_update(parameters, ['value'])
    record_outcomes(test, outcomes, spec.state, None, times, machines, rng)

def perturbation(param: SPSAParameter, c_compression: float) -> float:
    return max(param.c_value / c_compression, 0.0 if param.is_float else 0.5)

def simulate_tune(spec: DemoTune, run: SPSARun, parameters: list[SPSAParameter], rng: random.Random) -> list[int]:

    # Mirrors spsa_workload_assignment_dict and the Client's delta: each iteration
    # plays pairs_per pairs between value + flip * c and value - flip * c, then
    # moves every parameter by r * c * (wins - losses) * flip. The side nearer
    # the hidden optimum plays stronger, so the values drift towards it.

    outcomes: list[int] = []
    for step in range(spec.played):
        c_compression = (1 + step) ** run.gamma
        r_compression = (run.a_ratio * run.iterations + 1 + step) ** run.alpha

        flips = [rng.choice((-1, 1)) for _ in parameters]
        gain  = sum(
            (abs(param.value - flip * c - demo.optimum) - abs(param.value + flip * c - demo.optimum)) / (demo.max_value - demo.min_value)
            for param, demo, flip in zip(parameters, spec.parameters, flips)
            for c in [perturbation(param, c_compression)]
        )

        batch = simulate_pairs(120 * gain, spec.pairs_per, rng)
        wins, losses, _ = trinomial(tally(batch))
        outcomes.extend(batch)

        for param, flip in zip(parameters, flips):
            c           = perturbation(param, c_compression)
            r           = param.a_value / r_compression / c ** 2
            param.value = max(param.min_value, min(param.max_value, param.value + r * c * (wins - losses) * flip))

    return outcomes

def record_outcomes(
    test: Test, outcomes: list[int], state: str, bounds: tuple[float, float] | None,
    times: Schedule, machines: list[Machine], rng: random.Random,
) -> None:

    now     = timezone.now()
    penta   = tally(outcomes)
    playing = [machine for machine, (*_, hours_ago) in zip(machines, CPUS) if datetime.timedelta(hours=hours_ago) < now - times.started]
    for machine, share in zip(playing, split_pairs(penta, len(playing), rng)):
        if sum(share):
            create_result(test, machine, share, rng)

    wins, losses, draws = trinomial(penta)
    Test.objects.filter(id=test.id).update(
        wins=wins, losses=losses, draws=draws, LL=penta[0], LD=penta[1], DD=penta[2], DW=penta[3], WW=penta[4], games=2 * sum(penta),
        currentllr=PentanomialSPRT(penta, *bounds) if bounds and outcomes else 0.0,
        passed=state == 'passed', failed=state == 'failed', finished=state in FINISHED_STATES,
        creation=times.created, updated=times.ended if outcomes else times.created,
    )

    create_history(test, bounds, outcomes, times.started, times.ended, rng)
    Result.objects.filter(test=test).update(updated=times.ended)

def create_history(test, bounds, outcomes, started, ended, rng):

    # Pairs arrive at a jittered, roughly steady rate between started and ended,
    # sampled at evenly spaced snapshot times like the live recorder would keep

    points  = min(HISTORY_POINTS, len(outcomes))
    weights = [rng.uniform(0.6, 1.4) for _ in range(points)]
    total   = sum(weights)

    snapshots, cumulative = [], 0.0
    for index, weight in enumerate(weights, start=1):
        cumulative += weight
        played = len(outcomes) if index == points else round(len(outcomes) * cumulative / total)
        penta  = tally(outcomes[:played])
        wins, losses, draws = trinomial(penta)
        snapshots.append(WorkloadSnapshot(
            test=test, created=started + (ended - started) * (index / points),
            games=2 * played, wins=wins, losses=losses, draws=draws,
            LL=penta[0], LD=penta[1], DD=penta[2], DW=penta[3], WW=penta[4],
            llr=PentanomialSPRT(penta, *bounds) if bounds and played else 0.0,
        ))

    WorkloadSnapshot.objects.bulk_create(snapshots)

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
