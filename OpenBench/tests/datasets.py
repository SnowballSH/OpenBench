import datetime
import itertools
from dataclasses import dataclass

from django.contrib.auth.models import User
from django.utils import timezone

from OpenBench.models import (
    PGN,
    Engine,
    LogEvent,
    Machine,
    Network,
    Profile,
    Result,
    SPSAParameter,
    SPSARun,
    Test,
    WorkloadSnapshot,
)
from OpenBench.tests.fixtures import create_engine_config, ensure_book, system_info

ENGINES = ("Avalanche", "Other")
MODES = ("SPRT", "SPRT", "GAMES", "SPSA", "DATAGEN")
STATES = ("pending", "active", "active", "passed", "failed", "stopped", "deleted")


@dataclass(frozen=True)
class DatasetSize:
    users: int
    tests: int
    machines: int
    results: int
    events: int
    networks: int
    snapshots_per_test: int
    pgns: int
    spsa_parameters: int = 8


SMALL = DatasetSize(
    users=4,
    tests=80,
    machines=60,
    results=120,
    events=80,
    networks=10,
    snapshots_per_test=2,
    pgns=50,
)
LARGE = DatasetSize(
    users=20,
    tests=300,
    machines=2000,
    results=5000,
    events=1000,
    networks=100,
    snapshots_per_test=10,
    pgns=2000,
)


@dataclass(frozen=True)
class Dataset:
    users: list[User]
    tests: list[Test]
    machines: list[Machine]
    workload: Test


def state_fields(state: str) -> dict[str, bool]:
    return {
        "pending": {"approved": False},
        "active": {"approved": True},
        "passed": {"approved": True, "finished": True, "passed": True},
        "failed": {"approved": True, "finished": True, "failed": True},
        "stopped": {"approved": True, "finished": True},
        "deleted": {"approved": True, "finished": True, "deleted": True},
    }[state]


def create_users(count: int) -> list[User]:
    users = User.objects.bulk_create(
        [User(username=f"user{index}") for index in range(count)]
    )
    profiles = [
        Profile(user=user, enabled=True, approver=index == 0, games=index * 100)
        for index, user in enumerate(users)
    ]
    Profile.objects.bulk_create(profiles)
    return users


def create_networks(count: int) -> list[Network]:
    return Network.objects.bulk_create(
        [
            Network(
                sha256=f"{index:08X}",
                name=f"net-{index}",
                engine=ENGINES[index % len(ENGINES)],
                author="user0",
                default=index < len(ENGINES),
            )
            for index in range(count)
        ]
    )


def create_engines(count: int) -> list[Engine]:
    return Engine.objects.bulk_create(
        [
            Engine(
                name=f"branch-{index // 2}",
                source=f"https://api.github.com/repos/SnowballSH/Avalanche/zipball/{index:040x}",
                sha=f"{index:040x}",
                bench=index,
            )
            for index in range(count)
        ]
    )


def test_row(
    index: int, users: list[User], engines: list[Engine], networks: list[Network]
) -> Test:

    mode, state = MODES[index % len(MODES)], STATES[index % len(STATES)]
    dev, base = engines[2 * index], engines[2 * index + 1]
    engine = ENGINES[0]
    base_engine = ENGINES[1] if index % 11 == 0 else engine
    network = networks[index % len(networks)] if index % 3 == 0 and networks else None
    options = f"Threads={1 + index % 2} Hash=16"

    return Test(
        author=users[index % len(users)].username,
        book_name="UHO_Lichess_4852_v1.epd",
        info=f"workload {index}",
        dev=dev,
        dev_repo=dev.source,
        dev_engine=engine,
        dev_options=options,
        dev_time_control="8.0+0.08",
        base=base,
        base_repo=base.source,
        base_engine=base_engine,
        base_options=options,
        base_time_control="8.0+0.08",
        dev_network=network.sha256 if network else "",
        dev_netname=network.name if network else "",
        base_network="FFFFFFFF" if network else "",
        base_netname="base-net" if network else "",
        test_mode=mode,
        priority=index % 3,
        throughput=1000,
        max_games=1000,
        elolower=0.0,
        eloupper=3.0,
        alpha=0.05,
        beta=0.05,
        lowerllr=-2.94,
        upperllr=2.94,
        currentllr=(index % 50) / 10 - 2.5,
        games=index * 10,
        wins=index * 4,
        losses=index * 3,
        draws=index * 3,
        LL=index,
        LD=index,
        DD=index,
        DW=index,
        WW=index,
        **state_fields(state),
    )


def create_tests(
    size: DatasetSize, users: list[User], networks: list[Network]
) -> list[Test]:
    engines = create_engines(2 * size.tests)
    tests = Test.objects.bulk_create(
        [test_row(index, users, engines, networks) for index in range(size.tests)]
    )
    for index, test in enumerate(tests):
        Test.objects.filter(id=test.id).update(
            updated=timezone.now() - datetime.timedelta(minutes=index)
        )
    create_spsa_runs(
        [test for test in tests if test.test_mode == "SPSA"], size.spsa_parameters
    )
    return tests


def create_spsa_runs(tunes: list[Test], parameters: int) -> None:
    runs = SPSARun.objects.bulk_create(
        [
            SPSARun(
                tune=tune,
                reporting_type="BULK",
                distribution_type="SINGLE",
                alpha=0.602,
                gamma=0.101,
                iterations=100,
                pairs_per=8,
                a_ratio=0.1,
            )
            for tune in tunes
        ]
    )
    SPSAParameter.objects.bulk_create(
        [
            SPSAParameter(
                spsa_run=run,
                name=f"P{index}",
                index=index,
                value=10,
                is_float=False,
                start=10,
                min_value=0,
                max_value=100,
                c_end=1,
                r_end=0.002,
                c_value=1,
                a_value=1,
            )
            for run, index in itertools.product(runs, range(parameters))
        ]
    )


def create_machines(count: int, users: list[User]) -> list[Machine]:
    info = {
        **system_info(),
        "cpu_name": "Test CPU",
        "isa_name": "avx2",
        "machine_name": "box",
        "supported": list(ENGINES),
    }
    machines = Machine.objects.bulk_create(
        [
            Machine(
                user=users[index % len(users)],
                info=info,
                mnps=1.5,
                secret=f"secret-{index}",
            )
            for index in range(count)
        ]
    )
    stale = [machine.id for machine in machines[: count - count // 10]]
    Machine.objects.filter(id__in=stale).update(
        updated=timezone.now() - datetime.timedelta(days=1)
    )
    return machines


def create_results(
    count: int, workload: Test, tests: list[Test], machines: list[Machine]
) -> None:

    # Half of the Results land on one Workload, so its page carries a heavy table
    focused = [(workload, machine) for machine in machines[: count // 2]]
    spread = [
        (tests[index % len(tests)], machines[index % len(machines)])
        for index in range(count - len(focused))
    ]
    pairs = list(
        dict.fromkeys((test.id, machine.id) for test, machine in focused + spread)
    )

    Result.objects.bulk_create(
        [
            Result(
                test_id=test_id,
                machine_id=machine_id,
                games=20,
                wins=8,
                losses=6,
                draws=6,
                LL=1,
                LD=2,
                DD=4,
                DW=2,
                WW=1,
                dev_nodes=10**9,
                dev_time=10**4,
                dev_time_scaled=10**4,
                base_nodes=10**9,
                base_time=10**4,
                base_time_scaled=10**4,
            )
            for test_id, machine_id in pairs
        ]
    )


def create_events(count: int, tests: list[Test], machines: list[Machine]) -> None:
    LogEvent.objects.bulk_create(
        [
            LogEvent(
                author="user0",
                summary=f"event {index}",
                log_file="",
                machine_id=machines[index % len(machines)].id if index % 2 else 0,
                test_id=tests[index % len(tests)].id,
            )
            for index in range(count)
        ]
    )


def create_snapshots(per_test: int, tests: list[Test]) -> None:
    now = timezone.now()
    WorkloadSnapshot.objects.bulk_create(
        [
            WorkloadSnapshot(
                test=test,
                created=now - datetime.timedelta(hours=per_test - index),
                games=index * 10,
                LL=index,
                WW=index,
            )
            for test, index in itertools.product(tests, range(per_test))
        ]
    )


def create_pgns(count: int, tests: list[Test]) -> None:
    PGN.objects.bulk_create(
        [
            PGN(
                test_id=tests[index % len(tests)].id,
                result_id=index,
                book_index=index,
                processed=index % 4 != 0,
            )
            for index in range(count)
        ]
    )


def build_dataset(size: DatasetSize) -> Dataset:

    for engine in ENGINES:
        create_engine_config(engine)
    ensure_book()

    users = create_users(size.users)
    networks = create_networks(size.networks)
    tests = create_tests(size, users, networks)
    machines = create_machines(size.machines, users)
    workload = next(
        test
        for test in tests
        if test.test_mode == "SPRT" and test.approved and not test.finished
    )

    Machine.objects.filter(
        id__in=[machine.id for machine in machines[-size.machines // 10 :]]
    ).update(workload=workload.id)

    create_results(size.results, workload, tests, machines)
    create_events(size.events, tests, machines)
    create_snapshots(size.snapshots_per_test, tests)
    create_pgns(size.pgns, tests)

    return Dataset(users=users, tests=tests, machines=machines, workload=workload)
