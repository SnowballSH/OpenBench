import json
from collections.abc import Collection, Iterable
from datetime import datetime

from OpenBench.diagnosis.domain import FLEET_WINDOW, WorkerGroup
from OpenBench.fleet.pools import pool_label
from OpenBench.machine_info import int_of, text_of
from OpenBench.models import Machine

ELIGIBILITY_FIELDS = (
    'concurrency',
    'physical_cores',
    'supported',
    'only',
    'focus',
    'syzygy_max',
    'noisy',
    'os_name',
)
UNKNOWN_CPU = 'Unknown CPU'


def pool_of(machine: Machine) -> str:
    return pool_label(text_of(machine.info, 'machine_name'), text_of(machine.info, 'cpu_name'))


def group_key(machine: Machine, apart: Collection[int]) -> str:
    # One pool of machines that take the same workloads; one with a build failure is judged on its own
    info = machine.info if isinstance(machine.info, dict) else {}
    shape = [
        machine.user_id,
        machine.id if machine.id in apart else 0,
        pool_of(machine),
        info.get('cpu_name'),
        *(info.get(name) for name in ELIGIBILITY_FIELDS),
    ]
    return json.dumps(shape, sort_keys=True, default=str)


def group_label(machine: Machine) -> str:
    cpu = text_of(machine.info, 'cpu_name') or UNKNOWN_CPU
    name = text_of(machine.info, 'machine_name')
    pool = f'{pool_label(name, cpu)} on ' if name else ''
    return f'{pool}{cpu}, {int_of(machine.info, "concurrency")} threads ({machine.user.username})'


def host_of(machine: Machine) -> str:
    return machine.host_key or f'registration {machine.id}'


def group_workers(newest_first: Iterable[Machine], now: datetime, apart: Collection[int] = ()) -> list[WorkerGroup]:

    members: dict[str, list[Machine]] = {}
    for machine in newest_first:
        if now - machine.updated <= FLEET_WINDOW:
            members.setdefault(group_key(machine, apart), []).append(machine)

    return [
        WorkerGroup(
            label=group_label(machines[0]),
            machine=machines[0],
            hosts=len({host_of(machine) for machine in machines}),
            last_seen=machines[0].updated,
        )
        for machines in members.values()
    ]
