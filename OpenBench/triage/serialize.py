from typing import Any

from OpenBench import upstream
from OpenBench.triage.domain import Affected, GroupRow

type Payload = dict[str, Any]


def workload_json(row: GroupRow) -> Payload:

    test = row.workload
    if test is None:
        return {'id': row.group.test_id, 'exists': False}

    label = upstream.workload_label(test)
    return {
        'id': test.id,
        'exists': True,
        'url': upstream.workload_url(test),
        'title': label.title,
        'commits': label.commits,
        'engine': test.dev_engine,
        'time_control': test.dev_time_control,
        'finished': test.finished,
        'deleted': test.deleted,
        'games': test.games,
    }


def affected_json(affected: Affected | None) -> Payload | None:
    if affected is None:
        return None
    return {
        'registrations': affected.registrations,
        'hosts': affected.hosts,
        'pruned': affected.pruned,
        'sampled': affected.sampled,
        'pools': [{'label': pool.label, 'cpu': pool.cpu_name, 'hosts': pool.hosts} for pool in affected.pools],
    }


def bench_json(row: GroupRow) -> Payload | None:
    if row.bench is None:
        return None
    return {
        'reported': sorted(set(row.group.benches)),
        'expected': row.bench.expected,
        'difference': row.bench.difference,
    }


def group_json(row: GroupRow) -> Payload:
    group = row.group
    return {
        'workload': workload_json(row),
        'kind': group.signature.kind.value,
        'title': group.signature.title,
        'subject': group.signature.subject,
        'count': group.count,
        'first_seen': group.first_seen.isoformat(),
        'last_seen': group.last_seen.isoformat(),
        'status': row.verdict.standing.value,
        'reason': row.verdict.reason,
        'affected': affected_json(row.affected),
        'bench': bench_json(row),
        'latest_event': group.latest_event_id,
        'log_url': None if group.latest_log_event_id is None else f'/api/errors/{group.latest_log_event_id}/log/',
    }
