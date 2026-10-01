import hashlib
import json
from dataclasses import dataclass
from typing import NewType

from OpenBench.machine_info import int_of, text_of

HostKey = NewType('HostKey', str)

HOST_KEY_LENGTH = 32
MAC_BITS = 48
MAC_MULTICAST_BIT = 1 << 40


@dataclass(frozen=True, slots=True)
class HostIdentity:
    owner: str
    mac_address: str | None
    machine_name: str | None
    cpu_name: str | None
    os_name: str | None
    logical_cores: int
    physical_cores: int

    @property
    def key(self) -> HostKey:
        hardware = (self.cpu_name, self.os_name, self.logical_cores, self.physical_cores)
        parts = (
            ('mac', self.owner, self.mac_address, *hardware)
            if self.mac_address
            else ('hardware', self.owner, self.machine_name, *hardware)
        )
        return HostKey(hashlib.sha256(json.dumps(parts).encode()).hexdigest()[:HOST_KEY_LENGTH])


def stable_mac(value: object) -> str | None:
    # uuid.getnode() invents a random node with the multicast bit set when it finds no interface
    if not isinstance(value, str):
        return None
    try:
        node = int(value, 16)
    except ValueError:
        return None
    if not 0 < node < 1 << MAC_BITS or node & MAC_MULTICAST_BIT:
        return None
    return f'{node:012X}'


def host_identity(owner: str, info: object) -> HostIdentity:
    return HostIdentity(
        owner=owner,
        mac_address=stable_mac(info.get('mac_address')) if isinstance(info, dict) else None,
        machine_name=text_of(info, 'machine_name'),
        cpu_name=text_of(info, 'cpu_name'),
        os_name=text_of(info, 'os_name'),
        logical_cores=int_of(info, 'logical_cores'),
        physical_cores=int_of(info, 'physical_cores'),
    )


def host_key(owner: str, info: object) -> HostKey:
    return host_identity(owner, info).key
