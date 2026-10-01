from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import replace
from datetime import datetime
from functools import lru_cache
from typing import TypedDict

from django.db.models import CharField, Count, Max, Min, Q, QuerySet, Value
from django.db.models.fields.json import KT
from django.db.models.functions import Cast, Concat

from OpenBench.diagnosis.domain import RECENT_WINDOW
from OpenBench.diagnosis.sources import load_result_times
from OpenBench.fleet.pools import pool_label
from OpenBench.fleet.status import UNKNOWN, relative_age
from OpenBench.models import LogEvent, Machine, Test
from OpenBench.page_queries import dev_network_label
from OpenBench.triage.domain import (
    Affected,
    BenchMismatch,
    ErrorGroup,
    GroupRow,
    PoolCount,
    Standing,
    Verdict,
)
from OpenBench.triage.kinds import Signature, kind_condition, signature
from OpenBench.triage.query import ErrorQuery

RAW_GROUP_LIMIT = 500
MACHINE_SAMPLE = 900
BINARY_SHA_LENGTH = 8
STORED_LOG_NAME = Concat(Value('event'), Cast('id', CharField()), Value('.log'))


class SummaryRow(TypedDict):
    test_id: int
    summary: str
    count: int
    registrations: int
    first_seen: datetime
    last_seen: datetime
    latest_event_id: int
    latest_log_event_id: int | None


type GroupKey = tuple[int, tuple[str, str, str]]
type MachineFacts = tuple[str, str | None, str | None]


@lru_cache(maxsize=4096)
def cached_signature(summary: str) -> Signature:
    return signature(summary)


def error_events(query: ErrorQuery) -> QuerySet[LogEvent]:

    events = LogEvent.objects.filter(machine_id__gt=0)
    if query.workload is not None:
        events = events.filter(test_id=query.workload)
    if kinds := query.kinds:
        events = events.filter(kind_condition(kinds))
    if query.summaries:
        events = events.filter(summary__in=query.summaries)
    return events


def summary_rows(events: QuerySet[LogEvent]) -> list[SummaryRow]:
    grouped = events.values('test_id', 'summary').annotate(
        count=Count('id'),
        registrations=Count('machine_id', distinct=True),
        first_seen=Min('created'),
        last_seen=Max('created'),
        latest_event_id=Max('id'),
        latest_log_event_id=Max('id', filter=Q(log_file=STORED_LOG_NAME)),
    )
    return [SummaryRow(**row) for row in grouped.order_by('-last_seen', '-latest_event_id')[: RAW_GROUP_LIMIT + 1]]


def group_of(row: SummaryRow) -> ErrorGroup:
    found = cached_signature(row['summary'])
    return ErrorGroup(
        test_id=row['test_id'],
        signature=replace(found, bench=None),
        summaries=(row['summary'],),
        count=row['count'],
        registrations=row['registrations'],
        first_seen=row['first_seen'],
        last_seen=row['last_seen'],
        latest_event_id=row['latest_event_id'],
        latest_log_event_id=row['latest_log_event_id'],
        benches=() if found.bench is None else (found.bench,),
    )


def newest(a: int | None, b: int | None) -> int | None:
    return max((value for value in (a, b) if value is not None), default=None)


def merged(a: ErrorGroup, b: ErrorGroup) -> ErrorGroup:
    return replace(
        a,
        summaries=a.summaries + b.summaries,
        count=a.count + b.count,
        registrations=a.registrations + b.registrations,
        first_seen=min(a.first_seen, b.first_seen),
        last_seen=max(a.last_seen, b.last_seen),
        latest_event_id=max(a.latest_event_id, b.latest_event_id),
        latest_log_event_id=newest(a.latest_log_event_id, b.latest_log_event_id),
        benches=a.benches + b.benches,
    )


def merge_groups(rows: Iterable[SummaryRow]) -> list[ErrorGroup]:

    groups: dict[GroupKey, ErrorGroup] = {}
    for row in rows:
        group = group_of(row)
        key = (group.test_id, group.signature.key)
        groups[key] = merged(groups[key], group) if key in groups else group
    return sorted(groups.values(), key=lambda group: (group.last_seen, group.latest_event_id), reverse=True)


def load_workloads(ids: Iterable[int]) -> dict[int, Test]:
    tests = Test.objects.select_related('dev', 'base').annotate(dev_network_label=dev_network_label())
    return {test.id: test for test in tests.filter(id__in=set(ids))}


def latest_results(ids: Iterable[int]) -> dict[int, datetime | None]:
    return {test_id: reported for test_id, (reported, _) in load_result_times(sorted(set(ids))).items()}


def verdict(group: ErrorGroup, test: Test | None, latest_result: datetime | None, now: datetime) -> Verdict:

    if test is None:
        return Verdict(Standing.RESOLVED, 'workload no longer exists')

    if test.deleted:
        return Verdict(Standing.RESOLVED, 'workload deleted')

    if test.finished:
        return Verdict(Standing.RESOLVED, 'workload finished')

    if now - group.last_seen <= RECENT_WINDOW:
        return Verdict(Standing.HAPPENING, 'seen in the last 10 minutes')

    if latest_result is not None and latest_result > group.last_seen:
        return Verdict(Standing.RESOLVED, 'results arrived since')

    return Verdict(Standing.QUIET, 'no results since, none seen lately')


def expected_bench(subject: str, test: Test) -> int | None:

    for engine_name, engine in ((test.dev_engine, test.dev), (test.base_engine, test.base)):
        if subject.startswith(f'{engine_name}-{engine.sha.upper()[:BINARY_SHA_LENGTH]}'):
            return int(engine.bench)
    return None


def bench_mismatch(found: Signature, benches: Sequence[int], test: Test | None) -> BenchMismatch | None:
    if not benches:
        return None
    return BenchMismatch(got=benches[0], expected=expected_bench(found.subject, test) if test else None)


def group_row(group: ErrorGroup, test: Test | None, latest_result: datetime | None, now: datetime) -> GroupRow:
    return GroupRow(
        group=group,
        workload=test,
        verdict=verdict(group, test, latest_result, now),
        first_ago=relative_age(now - group.first_seen),
        last_ago=relative_age(now - group.last_seen),
        bench=bench_mismatch(group.signature, group.benches, test),
    )


def machine_sample(events: QuerySet[LogEvent], test_ids: set[int]) -> list[tuple[int, str, int]]:
    reported = events.filter(test_id__in=test_ids).values_list('test_id', 'summary', 'machine_id').distinct()
    return list(reported.order_by('-machine_id')[:MACHINE_SAMPLE])


def load_machine_facts(ids: set[int]) -> dict[int, MachineFacts]:
    machines = Machine.objects.filter(id__in=ids).values_list(
        'id', 'host_key', KT('info__machine_name'), KT('info__cpu_name')
    )
    return {machine_id: (host, name, cpu) for machine_id, host, name, cpu in machines}


def distinct_registrations(group: ErrorGroup, machine_ids: set[int], sampled: bool) -> int:
    counted_once = len(group.summaries) == 1 or sampled
    return group.registrations if counted_once else len(machine_ids)


def affected(group: ErrorGroup, machine_ids: set[int], facts: dict[int, MachineFacts], sampled: bool) -> Affected:

    known = {facts[machine_id] for machine_id in machine_ids if machine_id in facts}
    pools = Counter((pool_label(name, cpu), cpu or UNKNOWN) for _, name, cpu in known)
    return Affected(
        registrations=distinct_registrations(group, machine_ids, sampled),
        hosts=len(known),
        pools=tuple(PoolCount(label, cpu, hosts) for (label, cpu), hosts in pools.most_common()),
        pruned=sum(machine_id not in facts for machine_id in machine_ids),
        sampled=sampled,
    )


def with_affected(rows: Sequence[GroupRow], events: QuerySet[LogEvent]) -> tuple[GroupRow, ...]:

    if not rows:
        return ()

    sample = machine_sample(events, {row.group.test_id for row in rows})
    facts = load_machine_facts({machine_id for _, _, machine_id in sample})

    reporters: defaultdict[GroupKey, set[int]] = defaultdict(set)
    for test_id, summary, machine_id in sample:
        reporters[test_id, cached_signature(summary).key].add(machine_id)

    sampled = len(sample) == MACHINE_SAMPLE
    return tuple(
        replace(
            row,
            affected=affected(row.group, reporters[row.group.test_id, row.group.signature.key], facts, sampled),
        )
        for row in rows
    )


def group_rows(query: ErrorQuery, now: datetime) -> tuple[list[GroupRow], bool]:

    summaries = summary_rows(error_events(query))
    groups = merge_groups(summaries[:RAW_GROUP_LIMIT])
    if not groups:
        return [], False

    tests = load_workloads(group.test_id for group in groups)
    results = latest_results(tests)
    rows = [group_row(group, tests.get(group.test_id), results.get(group.test_id), now) for group in groups]
    shown = [row for row in rows if not (query.unresolved and row.verdict.resolved)]
    return shown, len(summaries) > RAW_GROUP_LIMIT
