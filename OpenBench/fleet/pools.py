import hashlib
import json
import re
from dataclasses import dataclass
from typing import NewType

PoolKey = NewType('PoolKey', str)

POOL_KEY_LENGTH = 12
POOL_KEY_PATTERN = re.compile(rf'[0-9a-f]{{{POOL_KEY_LENGTH}}}')
UNNAMED = 'Unknown'

UUID = r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}'
HEX_RUN = r'(?<![0-9a-z])(?=[0-9a-f]*[0-9])[0-9a-f]{8,}(?![0-9a-z])'
VOLATILE = re.compile(rf'{UUID}|{HEX_RUN}|[0-9]{{4,}}', re.IGNORECASE)
SLOT = re.compile(r':[0-9]+$')
ADJACENT_WILDCARDS = re.compile(r'\*(?:[-_.:]?\*)+')
LONG_ID = re.compile(rf'({UUID}|(?<![0-9a-z])[0-9a-f]{{16,}}(?![0-9a-z]))', re.IGNORECASE)
SHORT_ID_LENGTH = 8


@dataclass(frozen=True, slots=True)
class Pool:
    owner: str
    label: str
    cpu_name: str

    @property
    def key(self) -> PoolKey:
        parts = json.dumps([self.owner, self.label, self.cpu_name])
        return PoolKey(hashlib.sha256(parts.encode()).hexdigest()[:POOL_KEY_LENGTH])


def pool_label(machine_name: str | None, cpu_name: str | None) -> str:
    if not machine_name:
        return cpu_name or UNNAMED
    return ADJACENT_WILDCARDS.sub('*', VOLATILE.sub('*', SLOT.sub('', machine_name)))


def short_name(machine_name: str) -> str:
    return LONG_ID.sub(lambda match: f'{match.group(0)[:SHORT_ID_LENGTH]}…', machine_name)


def parse_pool_key(value: str | None) -> PoolKey | None:
    return PoolKey(value) if value and POOL_KEY_PATTERN.fullmatch(value) else None
