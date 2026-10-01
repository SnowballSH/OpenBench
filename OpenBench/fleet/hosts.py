import hashlib
import json
import re
from dataclasses import dataclass
from typing import NewType

from OpenBench.machine_info import int_of, text_of

HostKey = NewType('HostKey', str)

HOST_KEY_LENGTH = 32
MAC_BITS = 48
MAC_PATTERN = re.compile(r'[0-9A-Fa-f]{1,12}')
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
        # Containers report a new random address on every start, so a name outranks the address
        parts = (
            self.owner,
            self.machine_name,
            None if self.machine_name else self.mac_address,
            self.cpu_name,
            self.os_name,
            self.logical_cores,
            self.physical_cores,
        )
        return HostKey(hashlib.sha256(json.dumps(parts).encode()).hexdigest()[:HOST_KEY_LENGTH])


def stable_mac(value: object) -> str | None:
    # uuid.getnode() invents a random node with the multicast bit set when it finds no interface
    if not isinstance(value, str) or not MAC_PATTERN.fullmatch(value):
        return None
    node = int(value, 16)
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
