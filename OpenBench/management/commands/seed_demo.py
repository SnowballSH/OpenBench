# Populate an empty development database with a representative Server:
#
# >>> OPENBENCH_DEBUG=1 python3 manage.py seed_demo
#
# Creates accounts, an Engine, a Book, a fleet of Machines, and Workloads of
# every mode (SPRT, GAMES, SPSA, DATAGEN) in every state, each with per-Machine
# Results and a WorkloadSnapshot history. COMMIT_CHAIN adds commit-pinned
# tests like a lab agent creates: both branch names are 40-hex SHAs, each
# commit is tested at STC then LTC, and an accepted commit is the next base. Refuses to run without DEBUG, or
# against a database that already holds Workloads.

import datetime
import hashlib
import math
import random
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from django.conf import settings
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone

from OpenBench.fleet.hosts import host_key
from OpenBench.models import (
    Book,
    Engine,
    EngineConfig,
    Machine,
    Profile,
    Result,
    SPSAParameter,
    SPSARun,
    Test,
    WorkloadSnapshot,
)
from OpenBench.stats import PentanomialSPRT
from OpenBench.upstream import openbench_config

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

# This host runs the Client under a supervisor with --single-workload, so every
# workload it plays, and every start that finds no work, is a Machine row of its own
SUPERVISED_HOST = 2

IDLE_SESSIONS_MINUTES_AGO = (4, 9, 14, 45, 300)

SPLIT_SESSION_PAIRS = 400

# Ephemeral cloud jobs: each is a different VM named after its job id, registers
# once a minute while idle, plays at most one workload, and never returns
BATCH_CPU = 'AMD EPYC 9R14'
BATCH_THREADS = 8
BATCH_PLAYING_HOSTS = 4
BATCH_IDLE_HOSTS_HOURS_AGO = (2, 30, 50)
BATCH_IDLE_SESSIONS = 3

FINISHED_STATES = ('passed', 'failed', 'finished', 'stopped')

LLR_BOUND = 2.94

SPRT_BATCH = 50

SPRT_MAX_PAIRS = 40_000

DEFAULT_INFO = 'Seeded demonstration workload'

DEFAULT_SPEED = 1.02

STC = '8.0+0.08'

LTC = '40.0+0.4'

LTC_HASH_MB = 64


@dataclass(frozen=True)
class DemoWorkload:
    name: str
    mode: str
    elo: float
    pairs: int
    state: str
    priority: int = 0
    max_games: int = 0
    bounds: tuple[float, float] = (0.0, 3.0)
    tc: str = '8.0+0.08'
    threads: int = 1
    book: str = BOOK_NAME
    upload_pgns: str = 'FALSE'
    genfens_args: str = ''
    play_reverses: bool = False
    days_ago: float = 0.0
    duration_hours: float = 0.0
    author: int = 0
    base_name: str = 'master'
    dev_sha: str = ''
    base_sha: str = ''
    info: str = DEFAULT_INFO
    hash_mb: int = 0
    speed: float = DEFAULT_SPEED


@dataclass(frozen=True)
class DemoStage:
    tc: str
    state: str
    pairs: int = 0
    hash_mb: int = 0


@dataclass(frozen=True)
class DemoCommit:
    subject: str
    elo: float
    stages: tuple[DemoStage, ...]
    accepted: bool = False
    author: int = 1
    speed: float = 1.0


@dataclass(frozen=True)
class DemoParameter:
    name: str
    is_float: bool
    start: float
    min_value: float
    max_value: float
    c_end: float
    r_end: float
    optimum: float


@dataclass(frozen=True)
class DemoTune:
    name: str
    state: str
    iterations: int
    played: int
    parameters: tuple[DemoParameter, ...]
    pairs_per: int = 8
    alpha: float = 0.602
    gamma: float = 0.101
    a_ratio: float = 0.1
    reporting: str = SPSARun.SPSAReportingType.BATCHED
    distribution: str = SPSARun.SPSADistributionType.SINGLE
    tc: str = '8.0+0.08'
    priority: int = 0


WORKLOADS = [
    DemoWorkload('lmr-tweak', 'SPRT', 2.5, 5200, 'active', priority=1),
    DemoWorkload('history-bonus', 'SPRT', -1.0, 2600, 'active'),
    DemoWorkload('nezha-v2', 'SPRT', 6.0, 3100, 'passed', bounds=(0.0, 5.0)),
    DemoWorkload('aspiration-width', 'SPRT', -6.0, 1800, 'failed'),
    DemoWorkload('smp-scaling', 'GAMES', 12.0, 400, 'active', max_games=4000, tc='20.0+0.2', threads=4),
    DemoWorkload('qsearch-see', 'SPRT', 1.0, 0, 'pending'),
    DemoWorkload('ltc-regression', 'GAMES', -0.5, 1000, 'finished', max_games=2000, tc='40.0+0.4'),
    DemoWorkload(
        'nezha-v3-data',
        'DATAGEN',
        0.0,
        2400,
        'active',
        max_games=12000,
        tc='N=5000',
        book='NONE',
        upload_pgns='COMPACT',
        genfens_args='-randmoves 8',
    ),
    DemoWorkload(
        'nezha-v2-data',
        'DATAGEN',
        0.0,
        3000,
        'finished',
        max_games=6000,
        tc='N=5000',
        book='NONE',
        upload_pgns='COMPACT',
        play_reverses=True,
    ),
]

# Finished SPRT tests spread over the last six months, for the progress page
PAST_SPRTS = [
    DemoWorkload('see-pruning', 'SPRT', 16.0, 0, 'passed', days_ago=176, author=0),
    DemoWorkload('tt-aging', 'SPRT', -10.0, 0, 'failed', days_ago=168, author=1),
    DemoWorkload('nmp-verify', 'SPRT', 18.0, 0, 'passed', days_ago=160, author=2, bounds=(0.0, 5.0)),
    DemoWorkload('razoring', 'SPRT', 1.0, 1200, 'stopped', days_ago=151, author=0),
    DemoWorkload('killer-two', 'SPRT', -9.0, 0, 'failed', days_ago=143, author=1),
    DemoWorkload('singular-ext', 'SPRT', 20.0, 0, 'passed', days_ago=131, author=0),
    DemoWorkload('cont-history', 'SPRT', 14.0, 0, 'passed', days_ago=122, author=2),
    DemoWorkload('probcut', 'SPRT', -12.0, 0, 'failed', days_ago=110, author=0),
    DemoWorkload('simplify-eval', 'SPRT', 8.0, 0, 'passed', days_ago=101, author=1, bounds=(-3.0, 0.0)),
    DemoWorkload('lmp-table', 'SPRT', 15.0, 0, 'passed', days_ago=92, author=0),
    DemoWorkload('iir', 'SPRT', -8.0, 0, 'failed', days_ago=80, author=2),
    DemoWorkload('corr-history', 'SPRT', 16.0, 0, 'passed', days_ago=71, author=0),
    DemoWorkload('qs-futility', 'SPRT', 0.5, 900, 'stopped', days_ago=60, author=1),
    DemoWorkload('nezha-v1', 'SPRT', 22.0, 0, 'passed', days_ago=48, author=0, bounds=(0.0, 5.0)),
    DemoWorkload('capture-hist', 'SPRT', -11.0, 0, 'failed', days_ago=37, author=2),
    DemoWorkload('pv-lmr', 'SPRT', 13.0, 0, 'passed', days_ago=26, author=1),
    DemoWorkload('eval-cache', 'SPRT', 14.0, 0, 'passed', days_ago=15, author=0),
    DemoWorkload('mate-distance', 'SPRT', -9.0, 0, 'failed', days_ago=8, author=2),
]

# Commits in the order the lab agent proposed them. Each is tested against the newest accepted commit
# before it, so the two commits after the last accepted one are parallel candidates sharing a base.
COMMIT_CHAIN = (
    DemoCommit(
        'Scale late move reductions by history score',
        9.0,
        (DemoStage(STC, 'passed'), DemoStage(LTC, 'passed', hash_mb=LTC_HASH_MB)),
        accepted=True,
        speed=0.994,
    ),
    DemoCommit('Prune quiet moves with negative static exchange', -7.0, (DemoStage(STC, 'failed'),), speed=1.006),
    DemoCommit(
        'Extend the singular move search at high depth',
        8.0,
        (DemoStage(STC, 'passed'), DemoStage(STC, 'passed'), DemoStage(LTC, 'passed', hash_mb=LTC_HASH_MB)),
        accepted=True,
        speed=0.981,
    ),
    DemoCommit(
        'Pawn static-eval correction history (corrhist-pawn), indexed by pawn structure and side',
        8.0,
        (DemoStage(STC, 'passed'), DemoStage(LTC, 'active', pairs=1400, hash_mb=LTC_HASH_MB)),
        speed=0.968,
    ),
    DemoCommit('Widen aspiration windows after a fail high', 1.5, (DemoStage(STC, 'active', pairs=900),)),
)

CHAIN_ROOT = 'Seeded chain root'

SHARE_OF_BASE_USED = 0.9

MOVES_PER_SIDE = 70

UNTIMED_SECONDS_PER_SIDE = 10.0

SPEED_HOST_NOISE = 0.004

CHAIN_SPAN_DAYS = 5.0

CHAIN_SLOT_USED = 0.6

COMMIT_TAG = 'avl'

PROGRESS_CHECK_ELO = 14.0

PROGRESS_CHECK_GAMES = 3000

PROGRESS_CHECK_DAYS_AGO = 1.0

COMMIT_TAG_LENGTH = 12

SEARCH_PARAMETERS = (
    DemoParameter('LmrBase', True, 0.75, 0.25, 1.50, 0.08, 0.002, 0.92),
    DemoParameter('LmrDivisor', True, 2.25, 1.50, 3.50, 0.15, 0.002, 2.05),
    DemoParameter('RfpMargin', False, 75.0, 30.0, 150.0, 8.0, 0.002, 64.0),
    DemoParameter('NmpBaseReduction', False, 3.0, 1.0, 6.0, 0.5, 0.002, 4.0),
    DemoParameter('AspirationWindow', False, 12.0, 5.0, 40.0, 3.0, 0.002, 16.0),
    DemoParameter('HistoryDivisor', False, 8192.0, 2048.0, 16384.0, 600.0, 0.002, 10400.0),
)

EVAL_PARAMETERS = (
    DemoParameter('KnightMobility', False, 4.0, 0.0, 16.0, 1.0, 0.002, 5.0),
    DemoParameter('BishopPairBonus', False, 32.0, 0.0, 80.0, 4.0, 0.002, 41.0),
    DemoParameter('PassedPawnScale', True, 1.10, 0.50, 2.00, 0.06, 0.002, 1.24),
    DemoParameter('KingSafetyWeight', True, 0.85, 0.40, 1.60, 0.05, 0.002, 0.97),
)

TUNES = [
    DemoTune('search-tune', 'active', iterations=2500, played=900, parameters=SEARCH_PARAMETERS, priority=1),
    DemoTune(
        'eval-tune',
        'finished',
        iterations=1200,
        played=1200,
        parameters=EVAL_PARAMETERS,
        distribution=SPSARun.SPSADistributionType.MULTIPLE,
    ),
]


class Command(BaseCommand):
    help = 'Fill an empty DEBUG database with demonstration users, machines and workloads'

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument('--seed', type=int, default=20260929, help='Seed for the pseudo-random results')

    def handle(self, *args: Any, **options: Any) -> None:

        if not settings.DEBUG:
            raise CommandError('seed_demo only runs with OPENBENCH_DEBUG enabled')

        if Test.objects.exists():
            raise CommandError('The database already holds Workloads; seed_demo only fills an empty one')

        rng = random.Random(options['seed'])

        with transaction.atomic():
            users = create_users()
            create_engine_config()
            create_book()
            machines = create_machines(users)
            for spec in seeded_workloads():
                create_workload(spec, users[spec.author], machines, rng)
            for tune in TUNES:
                create_tune(tune, users[0], machines, rng)
            credit_profiles(users)
            settle_supervised_sessions(machines[SUPERVISED_HOST])
            create_batch_hosts(machines[SUPERVISED_HOST])
            assign_online_machines(machines)
            age_offline_machines(machines)

        self.stdout.write(
            f'Seeded {len(seeded_workloads()) + len(TUNES)} workloads on {len(machines)} machines '
            f'({Machine.objects.count()} registrations). '
            f'Log in as admin / {DEMO_PASSWORD}'
        )


def commit_sha(subject: str) -> str:
    return hashlib.sha1(subject.encode(), usedforsecurity=False).hexdigest()


def chain_workloads(
    commits: Sequence[DemoCommit], root: str = CHAIN_ROOT, span_days: float = CHAIN_SPAN_DAYS
) -> list[DemoWorkload]:

    # One SPRT per stage, oldest first, each in its own evenly spaced slot of span_days: a finished stage
    # ends before the next slot opens and a running one runs on until now, so every stage is created
    # after the test that accepted its base. The info is the lab agent's: the subject, then its commit tag.
    stages = sum(len(commit.stages) for commit in commits)
    slot_days = span_days / stages
    base = commit_sha(root)
    workloads: list[DemoWorkload] = []
    for commit in commits:
        dev = commit_sha(commit.subject)
        for stage in commit.stages:
            finished = stage.state in FINISHED_STATES
            workloads.append(
                DemoWorkload(
                    name=dev,
                    mode='SPRT',
                    elo=commit.elo,
                    pairs=stage.pairs,
                    state=stage.state,
                    tc=stage.tc,
                    days_ago=(days_ago := span_days - slot_days * len(workloads)),
                    duration_hours=24 * (slot_days * CHAIN_SLOT_USED if finished else days_ago),
                    author=commit.author,
                    base_name=base,
                    dev_sha=dev,
                    base_sha=base,
                    info=f'{commit.subject}\n{COMMIT_TAG}:{dev[:COMMIT_TAG_LENGTH]}',
                    hash_mb=stage.hash_mb,
                    speed=commit.speed,
                )
            )
        if commit.accepted:
            base = dev
    return workloads


def progress_checks(commits: Sequence[DemoCommit], root: str = CHAIN_ROOT) -> list[DemoWorkload]:

    # What the lab agent does not run but the progress page compares its chain against: a fixed-games
    # run of the newest accepted commit against the chain root, spanning every step between them
    newest = [commit for commit in commits if commit.accepted][-1]
    return [
        DemoWorkload(
            name='progress-check',
            mode='GAMES',
            elo=PROGRESS_CHECK_ELO,
            pairs=PROGRESS_CHECK_GAMES // 2,
            state='finished',
            max_games=PROGRESS_CHECK_GAMES,
            tc=LTC,
            days_ago=PROGRESS_CHECK_DAYS_AGO,
            duration_hours=6.0,
            base_name=commit_sha(root),
            dev_sha=commit_sha(newest.subject),
            base_sha=commit_sha(root),
            info=f'Progress since the chain root, up to: {newest.subject}',
            hash_mb=LTC_HASH_MB,
            speed=math.prod(commit.speed for commit in commits if commit.accepted),
        )
    ]


def seeded_workloads() -> list[DemoWorkload]:
    return [*WORKLOADS, *PAST_SPRTS, *chain_workloads(COMMIT_CHAIN), *progress_checks(COMMIT_CHAIN)]


def create_users() -> list[User]:
    specs = [('admin', True), ('lab-worker', False), ('home-worker', False)]
    users: list[User] = []
    for username, is_admin in specs:
        user = User.objects.create_user(username, '', DEMO_PASSWORD, is_staff=is_admin, is_superuser=is_admin)
        Profile.objects.create(user=user, enabled=True, approver=is_admin, superuser=is_admin)
        users.append(user)
    return users


def credit_profiles(users: Iterable[User]) -> None:
    for user in users:
        games = Result.objects.filter(machine__user=user).aggregate(total=Sum('games'))['total'] or 0
        tests = Test.objects.filter(author=user.username).count()
        Profile.objects.filter(user=user).update(games=games, tests=tests)


def create_engine_config() -> None:
    presets: dict[str, dict[str, dict[str, Any]]] = {
        'test_presets': {
            'default': {'book_name': BOOK_NAME, 'test_bounds': '[0.00, 3.00]', 'test_confidence': '[0.05, 0.05]'},
            'STC': {'both_options': 'Threads=1 Hash=16', 'both_time_control': STC},
            'LTC': {'both_options': f'Threads=1 Hash={LTC_HASH_MB}', 'both_time_control': LTC},
        },
        'tune_presets': {'default': {}},
        'datagen_presets': {'default': {}},
    }
    EngineConfig.objects.get_or_create(
        name='Avalanche',
        defaults={
            'nps': 1500000,
            'source': ENGINE_SOURCE,
            'build_path': '',
            'build_compilers': 'zig>=0.16.0',
            'build_cpuflags': '',
            'build_systems': 'Linux Darwin',
            'presets': presets,
        },
    )


def create_book() -> None:
    Book.objects.get_or_create(name=BOOK_NAME, defaults={'source': 'https://example.invalid/book.zip', 'sha': '0' * 64})


def create_machines(users: Sequence[User]) -> list[Machine]:
    machines: list[Machine] = []
    for index, (cpu_name, isa_name, os_name, threads, _) in enumerate(CPUS):
        owner = users[1 + index % 2]
        info = {
            'cpu_name': cpu_name,
            'isa_name': isa_name,
            'os_name': os_name,
            'os_ver': '',
            'concurrency': threads,
            'logical_cores': threads,
            'physical_cores': threads // 2,
            'cpu_flags': [],
            'compilers': {'Avalanche': ['zig', '0.16.0']},
            'tokens': {},
            'ram_total_mb': 65536,
            'syzygy_max': 0,
            'noisy': False,
            'sockets': 1,
            'machine_name': f'demo-{index + 1}',
            'mac_address': f'00163E{index + 1:06X}',
            'cli_options': cli_options(index, threads),
            'client_ver': openbench_config()['client_version'],
            'supported': ['Avalanche'],
        }
        mnps = round(1.2 + 0.4 * index, 2)
        machines.append(Machine.objects.create(user=owner, info=info, mnps=mnps, dev_mnps=mnps, base_mnps=mnps))
    return machines


def cli_options(index: int, threads: int) -> str:
    options = f'--threads {threads} --nsockets 1 --identity demo-{index + 1}'
    return f'{options} --single_workload' if index == SUPERVISED_HOST else options


def register_session(host: Machine) -> Machine:
    return Machine.objects.create(
        user=host.user, info=host.info, mnps=host.mnps, dev_mnps=host.dev_mnps, base_mnps=host.base_mnps
    )


def session_shares(share: Sequence[int]) -> list[list[int]]:
    if sum(share) < SPLIT_SESSION_PAIRS:
        return [list(share)]
    first = [count // 2 for count in share]
    return [first, [count - half for count, half in zip(share, first, strict=True)]]


def settle_supervised_sessions(host: Machine) -> None:
    now = timezone.now()
    for result in Result.objects.filter(machine__host_key=host.host_key).exclude(machine=host):
        Machine.objects.filter(id=result.machine_id).update(updated=min(result.updated, now), workload=result.test_id)
    for minutes_ago in IDLE_SESSIONS_MINUTES_AGO:
        idle = Machine.objects.create(user=host.user, info=host.info)
        Machine.objects.filter(id=idle.id).update(updated=now - datetime.timedelta(minutes=minutes_ago))


def batch_info(template: dict[str, Any], index: int) -> dict[str, Any]:
    identity = f'batch-{uuid.uuid5(uuid.NAMESPACE_DNS, f"demo-batch-{index}")}:0'
    return {
        **template,
        'cpu_name': BATCH_CPU,
        'isa_name': 'x86-64-avx512',
        'concurrency': BATCH_THREADS,
        'logical_cores': BATCH_THREADS,
        'physical_cores': BATCH_THREADS // 2,
        'machine_name': identity,
        'mac_address': f'0A58A9{index + 1:06X}',
        'cli_options': f'--threads {BATCH_THREADS} --nsockets 1 --identity {identity} --single_workload',
    }


def register_idle_sessions(owner: User, info: dict[str, Any], last_seen: datetime.datetime) -> None:
    for minutes_before in range(1, BATCH_IDLE_SESSIONS + 1):
        idle = Machine.objects.create(user=owner, info=info)
        Machine.objects.filter(id=idle.id).update(updated=last_seen - datetime.timedelta(minutes=minutes_before))


def create_batch_hosts(supervised: Machine) -> None:
    now = timezone.now()
    owner = supervised.user
    recent = Q(updated__lt=now - datetime.timedelta(hours=1), updated__gt=now - datetime.timedelta(days=6))
    played = Machine.objects.filter(
        recent, host_key=supervised.host_key, id__in=Result.objects.values('machine_id')
    ).order_by('-updated', '-id')

    for index, session in enumerate(played[:BATCH_PLAYING_HOSTS]):
        info = batch_info(supervised.info, index)
        Machine.objects.filter(id=session.id).update(info=info, host_key=host_key(owner.username, info))
        register_idle_sessions(owner, info, session.updated)

    for index, hours_ago in enumerate(BATCH_IDLE_HOSTS_HOURS_AGO, start=BATCH_PLAYING_HOSTS):
        register_idle_sessions(owner, batch_info(supervised.info, index), now - datetime.timedelta(hours=hours_ago))


def assign_online_machines(machines: list[Machine]) -> None:
    active = list(
        Test.objects.filter(approved=True, finished=False).order_by('-priority', 'id').values_list('id', flat=True)
    )
    online = [machine for machine, (*_, hours_ago) in zip(machines, CPUS, strict=True) if not hours_ago]
    for index, machine in enumerate(online):
        Machine.objects.filter(id=machine.id).update(workload=active[index % len(active)], updated=timezone.now())


def age_offline_machines(machines: Iterable[Machine]) -> None:
    now = timezone.now()
    for machine, (*_, hours_ago) in zip(machines, CPUS, strict=True):
        if not hours_ago:
            continue
        seen = now - datetime.timedelta(hours=hours_ago)
        Result.objects.filter(machine=machine, updated__gt=seen).update(updated=seen)
        last = (
            Result.objects.filter(machine=machine).order_by('-updated', '-id').values_list('test_id', flat=True).first()
        )
        Machine.objects.filter(id=machine.id).update(updated=seen, workload=last or 0)


def simulate_pairs(elo: float, pairs: int, rng: random.Random) -> list[int]:
    score = 1 / (1 + 10 ** (-elo / 400))
    draw = 0.55
    win_pair = (1 - draw) * score
    outcomes: list[int] = []
    for _ in range(pairs):
        first = 1.0 if rng.random() < win_pair else (0.5 if rng.random() < draw / (1 - win_pair) else 0.0)
        second = 1.0 if rng.random() < win_pair else (0.5 if rng.random() < draw / (1 - win_pair) else 0.0)
        outcomes.append(int(2 * (first + second)))
    return outcomes


def tally(outcomes: Iterable[int]) -> list[int]:
    penta = [0, 0, 0, 0, 0]
    for bucket in outcomes:
        penta[bucket] += 1
    return penta


def split_pairs(penta: Sequence[int], parts: int, rng: random.Random) -> list[list[int]]:
    weights = [rng.uniform(0.5, 2.0) for _ in range(parts)]
    shares = [[0] * 5 for _ in range(parts)]
    for bucket, count in enumerate(penta):
        for _ in range(count):
            shares[rng.choices(range(parts), weights)[0]][bucket] += 1
    return shares


@dataclass(frozen=True)
class Schedule:
    created: datetime.datetime
    started: datetime.datetime
    ended: datetime.datetime


def schedule(state: str, rng: random.Random, days_ago: float = 0.0, duration_hours: float = 0.0) -> Schedule:
    if days_ago and duration_hours:
        return exact_schedule(days_ago, duration_hours)
    if days_ago:
        return past_schedule(days_ago, rng)
    now = timezone.now()
    created = now - datetime.timedelta(hours=rng.uniform(1, 72))
    started = created + datetime.timedelta(minutes=rng.uniform(2, 10))
    ended = started + (now - started) * (rng.uniform(0.3, 0.9) if state in FINISHED_STATES else 1.0)
    return Schedule(created, started, ended)


def exact_schedule(days_ago: float, duration_hours: float) -> Schedule:
    created = timezone.now() - datetime.timedelta(days=days_ago)
    started = created + datetime.timedelta(minutes=5)
    return Schedule(created, started, min(timezone.now(), started + datetime.timedelta(hours=duration_hours)))


def past_schedule(days_ago: float, rng: random.Random) -> Schedule:
    created = timezone.now() - datetime.timedelta(days=days_ago, hours=rng.uniform(0, 12))
    started = created + datetime.timedelta(minutes=rng.uniform(2, 10))
    ended = started + datetime.timedelta(hours=rng.uniform(2, 18))
    return Schedule(created, started, ended)


def create_engine(name: str, rng: random.Random, sha: str = '') -> Engine:
    if sha:
        # A pinned commit benches the same in every Workload that builds it
        bench = 2_000_000 + int(sha[:8], 16) % 2_000_000
    else:
        sha = f'{rng.getrandbits(160):040x}'
        bench = rng.randint(2_000_000, 4_000_000)
    source = ENGINE_SOURCE.replace('github.com', 'api.github.com/repos') + f'/zipball/{sha}'
    return Engine.objects.create(name=name, source=source, sha=sha, bench=bench)


def create_workload(spec: DemoWorkload, author: User, machines: list[Machine], rng: random.Random) -> None:

    dev = create_engine(spec.name, rng, spec.dev_sha)
    base = create_engine(spec.base_name, rng, spec.base_sha)
    options = f'Threads={spec.threads} Hash={spec.hash_mb or 16 * spec.threads}'
    times = schedule(spec.state, rng, spec.days_ago, spec.duration_hours)
    is_sprt = spec.mode == 'SPRT'
    is_data = spec.mode == 'DATAGEN'
    sprt = {
        'elolower': spec.bounds[0],
        'eloupper': spec.bounds[1],
        'alpha': 0.05,
        'beta': 0.05,
        'lowerllr': -LLR_BOUND,
        'upperllr': LLR_BOUND,
    }

    test = Test.objects.create(
        author=author.username,
        book_name=spec.book,
        upload_pgns=spec.upload_pgns,
        dev=dev,
        dev_repo=ENGINE_SOURCE,
        dev_engine='Avalanche',
        dev_options=options,
        dev_network='BCF481FD',
        dev_netname='nezha',
        dev_time_control=spec.tc,
        base=base,
        base_repo=ENGINE_SOURCE,
        base_engine='Avalanche',
        base_options=options,
        base_network='BCF481FD',
        base_netname='nezha',
        base_time_control=spec.tc,
        test_mode=spec.mode,
        max_games=spec.max_games,
        priority=spec.priority,
        throughput=1000,
        genfens_args=spec.genfens_args,
        play_reverses=spec.play_reverses,
        use_tri=is_data and not spec.play_reverses,
        use_penta=not is_data or spec.play_reverses,
        approved=spec.state != 'pending',
        info=spec.info,
        **(sprt if is_sprt else {}),
    )

    bounds = spec.bounds if is_sprt else None
    finished = spec.state in FINISHED_STATES
    decided = is_sprt and spec.state in ('passed', 'failed')
    outcomes = sprt_to_verdict(spec, rng) if decided else simulate_pairs(spec.elo, spec.pairs, rng)
    record_outcomes(test, outcomes, finished, bounds, times, machines, rng, spec.speed)

    test.refresh_from_db()
    reached = 'passed' if test.passed else 'failed' if test.failed else 'stopped'
    if is_sprt and finished and spec.state != reached:
        raise CommandError(f'{spec.name} was meant to have {spec.state}; choose another elo')


def sprt_to_verdict(spec: DemoWorkload, rng: random.Random) -> list[int]:

    # Plays until the LLR leaves the bounds, like update_test would stop it
    outcomes: list[int] = []
    penta = [0, 0, 0, 0, 0]
    while abs(PentanomialSPRT(penta, *spec.bounds) if outcomes else 0.0) <= LLR_BOUND:
        if len(outcomes) >= SPRT_MAX_PAIRS:
            raise CommandError(f'{spec.name} did not reach a verdict in {SPRT_MAX_PAIRS} pairs; choose another elo')
        batch = simulate_pairs(spec.elo, SPRT_BATCH, rng)
        penta = [total + added for total, added in zip(penta, tally(batch), strict=True)]
        outcomes.extend(batch)
    return outcomes


def create_tune(spec: DemoTune, author: User, machines: list[Machine], rng: random.Random) -> None:

    engine = create_engine(spec.name, rng)
    times = schedule(spec.state, rng)
    options = 'Threads=1 Hash=16'

    test = Test.objects.create(
        author=author.username,
        book_name=BOOK_NAME,
        dev=engine,
        dev_repo=ENGINE_SOURCE,
        dev_engine='Avalanche',
        dev_options=options,
        dev_network='BCF481FD',
        dev_netname='nezha',
        dev_time_control=spec.tc,
        base=engine,
        base_repo=ENGINE_SOURCE,
        base_engine='Avalanche',
        base_options=options,
        base_network='BCF481FD',
        base_netname='nezha',
        base_time_control=spec.tc,
        test_mode='SPSA',
        workload_size=spec.pairs_per,
        priority=spec.priority,
        throughput=1000,
        approved=True,
        info='Seeded demonstration tune',
    )

    run = SPSARun.objects.create(
        tune=test,
        reporting_type=spec.reporting,
        distribution_type=spec.distribution,
        alpha=spec.alpha,
        gamma=spec.gamma,
        iterations=spec.iterations,
        pairs_per=spec.pairs_per,
        a_ratio=spec.a_ratio,
    )

    parameters = SPSAParameter.objects.bulk_create(
        [
            SPSAParameter(
                spsa_run=run,
                name=param.name,
                index=index,
                value=param.start,
                is_float=param.is_float,
                start=param.start,
                min_value=param.min_value,
                max_value=param.max_value,
                c_end=param.c_end,
                r_end=param.r_end,
                c_value=param.c_end * spec.iterations**spec.gamma,
                a_value=param.r_end * param.c_end**2 * (spec.a_ratio * spec.iterations + spec.iterations) ** spec.alpha,
            )
            for index, param in enumerate(spec.parameters)
        ]
    )

    outcomes = simulate_tune(spec, run, parameters, rng)
    SPSAParameter.objects.bulk_update(parameters, ['value'])
    record_outcomes(test, outcomes, spec.state in FINISHED_STATES, None, times, machines, rng)


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
        gain = sum(
            (abs(param.value - flip * c - demo.optimum) - abs(param.value + flip * c - demo.optimum))
            / (demo.max_value - demo.min_value)
            for param, demo, flip in zip(parameters, spec.parameters, flips, strict=True)
            for c in [perturbation(param, c_compression)]
        )

        batch = simulate_pairs(120 * gain, spec.pairs_per, rng)
        wins, losses, _ = trinomial(tally(batch))
        outcomes.extend(batch)

        for param, flip in zip(parameters, flips, strict=True):
            c = perturbation(param, c_compression)
            r = param.a_value / r_compression / c**2
            param.value = max(param.min_value, min(param.max_value, param.value + r * c * (wins - losses) * flip))

    return outcomes


def record_outcomes(
    test: Test,
    outcomes: list[int],
    finished: bool,
    bounds: tuple[float, float] | None,
    times: Schedule,
    machines: list[Machine],
    rng: random.Random,
    speed: float = DEFAULT_SPEED,
) -> None:

    now = timezone.now()
    penta = tally(outcomes)
    playing = [
        machine
        for machine, (*_, hours_ago) in zip(machines, CPUS, strict=True)
        if datetime.timedelta(hours=hours_ago) < now - times.started
    ]
    for machine, share in zip(playing, split_pairs(penta, len(playing), rng), strict=True):
        if not sum(share):
            continue
        if machine is not machines[SUPERVISED_HOST]:
            create_result(test, machine, share, rng, speed)
            continue
        first, *later = session_shares(share)
        create_result(test, register_session(machine), first, rng, speed)
        for session_share in later:
            create_result(test, register_session(machine), session_share, random.Random(test.id), speed)

    wins, losses, draws = trinomial(penta)
    llr = PentanomialSPRT(penta, *bounds) if bounds and outcomes else 0.0
    passed, failed = verdict(test.test_mode, finished, llr, wins, losses)
    Test.objects.filter(id=test.id).update(
        wins=wins,
        losses=losses,
        draws=draws,
        LL=penta[0],
        LD=penta[1],
        DD=penta[2],
        DW=penta[3],
        WW=penta[4],
        games=2 * sum(penta),
        currentllr=llr,
        passed=passed,
        failed=failed,
        finished=finished,
        creation=times.created,
        updated=times.ended if outcomes else times.created,
    )

    create_history(test, bounds, outcomes, times.started, times.ended, rng)
    Result.objects.filter(test=test).update(updated=times.ended)


def verdict(mode: str, finished: bool, llr: float, wins: int, losses: int) -> tuple[bool, bool]:

    # The flags update_test would have set on the report that finished the Workload
    if not finished:
        return False, False

    match mode:
        case 'SPRT':
            return llr > LLR_BOUND, llr < -LLR_BOUND
        case 'GAMES':
            return wins >= losses, wins < losses
        case 'DATAGEN':
            return True, False
        case _:
            return False, False


def create_history(
    test: Test,
    bounds: tuple[float, float] | None,
    outcomes: list[int],
    started: datetime.datetime,
    ended: datetime.datetime,
    rng: random.Random,
) -> None:

    # Pairs arrive at a jittered, roughly steady rate between started and ended,
    # sampled at evenly spaced snapshot times like the live recorder would keep

    points = min(HISTORY_POINTS, len(outcomes))
    weights = [rng.uniform(0.6, 1.4) for _ in range(points)]
    total = sum(weights)

    snapshots: list[WorkloadSnapshot] = []
    cumulative, counted, penta = 0.0, 0, [0, 0, 0, 0, 0]
    for index, weight in enumerate(weights, start=1):
        cumulative += weight
        played = len(outcomes) if index == points else round(len(outcomes) * cumulative / total)
        penta = [so_far + added for so_far, added in zip(penta, tally(outcomes[counted:played]), strict=True)]
        counted = played
        wins, losses, draws = trinomial(penta)
        snapshots.append(
            WorkloadSnapshot(
                test=test,
                created=started + (ended - started) * (index / points),
                games=2 * played,
                wins=wins,
                losses=losses,
                draws=draws,
                LL=penta[0],
                LD=penta[1],
                DD=penta[2],
                DW=penta[3],
                WW=penta[4],
                llr=PentanomialSPRT(penta, *bounds) if bounds and played else 0.0,
            )
        )

    WorkloadSnapshot.objects.bulk_create(snapshots)


def trinomial(penta: Sequence[int]) -> tuple[int, int, int]:
    wins = 2 * penta[4] + penta[3]
    losses = 2 * penta[0] + penta[1]
    return wins, losses, 2 * sum(penta) - wins - losses


def seconds_per_side(time_control: str) -> float:
    base, _, increment = time_control.partition('+')
    try:
        return SHARE_OF_BASE_USED * float(base) + MOVES_PER_SIDE * float(increment or 0)
    except ValueError:
        return UNTIMED_SECONDS_PER_SIDE


def create_result(test: Test, machine: Machine, penta: Sequence[int], rng: random.Random, speed: float) -> None:
    pairs = sum(penta)
    wins, losses, draws = trinomial(penta)
    draw = rng.randint(9_000, 11_000)
    jitter = (draw - 10_000) / 1_000
    time = int(2 * pairs * seconds_per_side(test.dev_time_control) * draw / 10)
    nodes = int(time * machine.mnps * 1000)
    host_speed = speed * (1 + SPEED_HOST_NOISE * jitter)
    Result.objects.create(
        test=test,
        machine=machine,
        games=2 * pairs,
        wins=wins,
        losses=losses,
        draws=draws,
        LL=penta[0],
        LD=penta[1],
        DD=penta[2],
        DW=penta[3],
        WW=penta[4],
        dev_nodes=nodes,
        dev_time=time,
        dev_time_scaled=time,
        base_nodes=int(nodes / host_speed),
        base_time=time,
        base_time_scaled=time,
    )
