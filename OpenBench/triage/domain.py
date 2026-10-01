from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from OpenBench.models import Test
from OpenBench.triage.kinds import Signature
from OpenBench.triage.query import events_query


class Standing(StrEnum):
    HAPPENING = 'happening'
    QUIET = 'quiet'
    RESOLVED = 'resolved'

    @property
    def label(self) -> str:
        return {
            Standing.HAPPENING: 'Still happening',
            Standing.QUIET: 'Quiet',
            Standing.RESOLVED: 'Resolved',
        }[self]

    @property
    def badge(self) -> str:
        return {Standing.HAPPENING: 'badge-fail', Standing.QUIET: 'badge-warn', Standing.RESOLVED: 'badge-pass'}[self]


@dataclass(frozen=True, slots=True)
class Verdict:
    standing: Standing
    reason: str

    @property
    def resolved(self) -> bool:
        return self.standing is Standing.RESOLVED


@dataclass(frozen=True, slots=True)
class PoolCount:
    label: str
    cpu_name: str
    hosts: int


@dataclass(frozen=True, slots=True)
class Affected:
    registrations: int
    hosts: int
    pools: tuple[PoolCount, ...]
    pruned: int
    sampled: bool


@dataclass(frozen=True, slots=True)
class BenchMismatch:
    got: int
    expected: int | None

    @property
    def difference(self) -> int | None:
        return None if self.expected is None else self.got - self.expected


@dataclass(frozen=True, slots=True)
class ErrorGroup:
    test_id: int
    signature: Signature
    summaries: tuple[str, ...]
    count: int
    registrations: int
    first_seen: datetime
    last_seen: datetime
    latest_event_id: int
    latest_log_event_id: int | None
    benches: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class GroupRow:
    group: ErrorGroup
    workload: Test | None
    verdict: Verdict
    first_ago: str
    last_ago: str
    bench: BenchMismatch | None
    affected: Affected | None = None

    @property
    def log_event_id(self) -> int | None:
        return self.group.latest_log_event_id

    @property
    def events_querystring(self) -> str:
        return events_query(self.group.test_id, self.group.summaries).querystring
