from collections.abc import Sequence
from datetime import datetime, timedelta

from OpenBench.diagnosis.domain import (
    RECENT_WINDOW,
    SEVERITY,
    SHOWN_ERRORS,
    SHOWN_GROUPS,
    SHOWN_RIVALS,
    STALL_FACTOR,
    STALL_FLOOR,
    Activity,
    Diagnosis,
    DiagnosisState,
    Evidence,
    EvidenceKind,
    Link,
    Obstacle,
    ObstacleKind,
    WorkerGroup,
)
from OpenBench.diagnosis.standing import FleetJudge, Rival, Standing, StandingKind
from OpenBench.insights.domain import WorkloadStatus
from OpenBench.insights.listing import format_duration
from OpenBench.insights.sources import workload_facts
from OpenBench.machine_info import int_of
from OpenBench.models import LogEvent, Machine, Test

type Judged = tuple[WorkerGroup, tuple[Obstacle, ...]]

OBSTACLE_BRIEF: dict[ObstacleKind, str] = {
    ObstacleKind.ENGINE_UNSUPPORTED: 'they cannot build the engine',
    ObstacleKind.ONLY_EXCLUDES: 'they are restricted to other engines',
    ObstacleKind.BLACKLISTED: 'they failed to build it',
    ObstacleKind.SYZYGY: 'they lack the Syzygy tablebases',
    ObstacleKind.NOISY: 'they are noisy machines',
    ObstacleKind.THREADS: 'they have too few threads',
    ObstacleKind.UNREADABLE: 'they cannot be evaluated',
}
VISIBILITY_LIMIT = (
    'The server only sees a worker when it registers, takes a workload or reports: an idle worker that is '
    'polling, and a --blacklist given on its command line, are invisible here.'
)
BUSY_NOTE = 'A busy worker asks for work again only after it finishes its current batch.'
FINISHED_BRIEF = 'finished'
FINISHED_TEXT: dict[WorkloadStatus, str] = {
    WorkloadStatus.STOPPED: 'was stopped',
    WorkloadStatus.DELETED: 'is deleted',
}
SUBJECT_LENGTH = 60


def ago(now: datetime, then: datetime) -> str:
    return f'{format_duration((now - then).total_seconds())} ago'


def plural(count: int, noun: str) -> str:
    return f'{count:,} {noun}' if count == 1 else f'{count:,} {noun}s'


def workload_link(workload: Test) -> Link:
    return Link(f'/{workload.workload_type_str()}/{workload.id}/', f'Workload {workload.id}')


def machine_link(machine_id: int) -> Link:
    return Link(f'/machines/{machine_id}/', f'Machine {machine_id}')


def event_link(event: LogEvent) -> Link:
    return Link(f'/event/{event.id}/', f'Log {event.id}') if event.log_file else Link('/errors/', 'Errors')


def verdict(state: DiagnosisState, headline: str, brief: str, evidence: Sequence[Evidence] = ()) -> Diagnosis:
    return Diagnosis(state, SEVERITY[state], headline, brief, list(evidence))


def settled_status(workload: Test) -> WorkloadStatus:
    return workload_facts(workload).status


def error_evidence(errors: Sequence[LogEvent], now: datetime) -> list[Evidence]:
    return [
        Evidence(
            EvidenceKind.ERROR,
            f'Machine {event.machine_id} reported "{event.summary}" {ago(now, event.created)}.',
            event_link(event),
        )
        for event in errors[:SHOWN_ERRORS]
    ]


def unresolved_errors(activity: Activity) -> list[LogEvent]:
    since = activity.last_result_at
    return [event for event in activity.errors if since is None or event.created > since]


def last_result_evidence(activity: Activity, now: datetime) -> Evidence:
    if activity.last_result_at is None:
        return Evidence(EvidenceKind.LAST_RESULT, 'No result has been reported yet.')
    return Evidence(EvidenceKind.LAST_RESULT, f'The last result arrived {ago(now, activity.last_result_at)}.')


def expected_game_seconds(workload: Test) -> float | None:
    try:
        base, _, increment = workload.dev_time_control.partition('+')
        return 2 * (float(base) + 80 * float(increment or 0))
    except ValueError:
        return None


def stall_after(workload: Test) -> timedelta:
    expected = expected_game_seconds(workload)
    return max(STALL_FLOOR, timedelta(seconds=STALL_FACTOR * expected)) if expected else STALL_FLOOR


def reports_only_at_the_end(workload: Test) -> bool:
    return workload.test_mode == 'SPSA' and workload.spsa_run.reporting_type == 'BULK'


def diagnose_finished(workload: Test, activity: Activity, now: datetime) -> Diagnosis:
    status = settled_status(workload)
    return verdict(
        DiagnosisState.FINISHED,
        f'Not applicable: this workload {FINISHED_TEXT.get(status, "has finished")}, so no worker will take it.',
        FINISHED_BRIEF,
        error_evidence(activity.errors, now) if status == WorkloadStatus.STOPPED else [],
    )


def diagnose_unknown() -> Diagnosis:
    return verdict(
        DiagnosisState.UNKNOWN,
        'Unknown: the server cannot tell what this workload is waiting for.',
        'unknown',
    )


def diagnose_pending() -> Diagnosis:
    return verdict(
        DiagnosisState.AWAITING_APPROVAL,
        'Awaiting approval: workers only receive approved workloads.',
        'awaiting approval',
    )


def diagnose_held(workload: Test, holders: Sequence[Machine], activity: Activity, now: datetime) -> Diagnosis:

    threads = sum(int_of(machine.info, 'concurrency') for machine in holders)
    workers = f'{plural(len(holders), "worker")} ({plural(threads, "thread")})'
    last_result = last_result_evidence(activity, now)
    evidence = [
        *(
            Evidence(
                EvidenceKind.WORKERS,
                f'Machine {machine.id} checked in {ago(now, machine.updated)}.',
                machine_link(machine.id),
            )
            for machine in holders[:SHOWN_GROUPS]
        ),
        last_result,
        *error_evidence(unresolved_errors(activity), now),
    ]
    progress = max(
        (moment for moment in (activity.last_result_at, activity.last_assigned_at) if moment is not None),
        default=None,
    )
    limit = stall_after(workload)

    if progress is not None and now - progress > limit and not reports_only_at_the_end(workload):
        quiet = format_duration((now - progress).total_seconds())
        return verdict(
            DiagnosisState.STALLED,
            f'Stalled: {workers} still hold this workload and check in, but nothing has been reported for {quiet} '
            f'(a result is expected within {format_duration(limit.total_seconds())}).',
            f'stalled: no result for {quiet}',
            evidence,
        )

    return verdict(
        DiagnosisState.RUNNING,
        f'Running normally: {workers} on it. {last_result.text}',
        f'running on {plural(len(holders), "worker")}',
        evidence,
    )


def diagnose_starting(activity: Activity, errors: Sequence[LogEvent], now: datetime) -> Diagnosis | None:

    failed = {event.machine_id for event in errors}
    preparing = [item for item in activity.preparing if item.machine_id not in failed]
    if not preparing:
        return None

    newest = max(item.assigned_at for item in preparing)
    return verdict(
        DiagnosisState.STARTING,
        f'Starting: handed to {plural(len(preparing), "worker")}, most recently {ago(now, newest)}, with nothing '
        'reported yet. Workers send no heartbeat while they build and benchmark the engines.',
        f'starting on {plural(len(preparing), "worker")}',
        [
            Evidence(
                EvidenceKind.ASSIGNMENT,
                f'Machine {item.machine_id} took it {ago(now, item.assigned_at)}.',
                machine_link(item.machine_id),
            )
            for item in preparing[:SHOWN_GROUPS]
        ],
    )


def group_text(group: WorkerGroup, now: datetime) -> str:
    return f'{group.label}: {plural(group.hosts, "host")}, last seen {ago(now, group.last_seen)}'


def ineligible_evidence(judged: Sequence[Judged], now: datetime) -> list[Evidence]:
    return [
        Evidence(
            EvidenceKind.INELIGIBLE,
            f'{group_text(group, now)}. It {"; it ".join(obstacle.detail for obstacle in found)}.',
            machine_link(group.machine.id),
        )
        for group, found in judged[:SHOWN_GROUPS]
    ]


def eligible_evidence(groups: Sequence[WorkerGroup], now: datetime) -> list[Evidence]:
    return [
        Evidence(
            EvidenceKind.ELIGIBLE,
            f'{group_text(group, now)}. It can take this workload.',
            machine_link(group.machine.id),
        )
        for group in groups[:SHOWN_GROUPS]
    ]


def last_worker_evidence(machine: Machine | None, now: datetime) -> Evidence:
    if machine is None:
        return Evidence(EvidenceKind.LAST_WORKER, 'No worker has ever registered with this server.')
    return Evidence(
        EvidenceKind.LAST_WORKER,
        f'The last worker seen was machine {machine.id} ({machine.user.username}), {ago(now, machine.updated)}.',
        machine_link(machine.id),
    )


def most_common_obstacle(judged: Sequence[Judged]) -> ObstacleKind:
    counts: dict[ObstacleKind, int] = {}
    for group, found in judged:
        counts[found[0].kind] = counts.get(found[0].kind, 0) + group.hosts
    return max(counts, key=lambda kind: counts[kind])


def diagnose_no_workers(
    last_seen: Machine | None, eligible: Sequence[WorkerGroup], errors: Sequence[LogEvent], now: datetime
) -> Diagnosis:

    window = format_duration(RECENT_WINDOW.total_seconds())
    quiet = f'for {format_duration((now - last_seen.updated).total_seconds())}' if last_seen else 'yet'
    return verdict(
        DiagnosisState.NO_WORKERS,
        f'No workers: nothing has registered, taken work or reported in the last {window}. '
        + (
            f'The last worker was seen {ago(now, last_seen.updated)}.'
            if last_seen
            else 'No worker has ever registered.'
        ),
        f'no workers {quiet}',
        [
            last_worker_evidence(last_seen, now),
            *eligible_evidence(eligible, now),
            *error_evidence(errors, now),
            Evidence(EvidenceKind.LIMIT, VISIBILITY_LIMIT),
        ],
    )


def diagnose_no_eligible(
    blocked: Sequence[Judged], absent: Sequence[WorkerGroup], errors: Sequence[LogEvent], now: datetime
) -> Diagnosis:

    kind = most_common_obstacle(blocked)
    if kind == ObstacleKind.UNREADABLE and not absent:
        return verdict(
            DiagnosisState.UNKNOWN,
            'Unknown: the server cannot evaluate this workload against the workers it has seen.',
            'unknown',
            ineligible_evidence(blocked, now),
        )

    kinds = plural(len(blocked), 'kind')
    scope = (
        f'the {kinds} of worker seen in the last {format_duration(RECENT_WINDOW.total_seconds())} cannot take it, and '
        f'the workers that can were last seen {ago(now, absent[0].last_seen)}'
        if absent
        else f'none of the {kinds} of worker seen in the last 24h can take it'
    )
    return verdict(
        DiagnosisState.NO_ELIGIBLE_WORKERS,
        f'No eligible worker: {scope}. Most often {OBSTACLE_BRIEF[kind]}.',
        f'no eligible worker: {OBSTACLE_BRIEF[kind]}',
        [
            *ineligible_evidence(blocked, now),
            *eligible_evidence(absent, now),
            *error_evidence(errors, now),
            Evidence(EvidenceKind.LIMIT, VISIBILITY_LIMIT),
        ],
    )


def rival_text(rival: Rival) -> str:
    subject = rival.workload.info.strip().partition('\n')[0].strip()
    return f'#{rival.workload.id} ({subject[:SUBJECT_LENGTH]})' if subject else f'#{rival.workload.id}'


def rival_evidence(workload: Test, standing: Standing) -> list[Evidence]:

    def text(rival: Rival) -> str:
        other = rival.workload
        if standing.kind == StandingKind.OUTRANKED:
            gap = other.priority - workload.priority
            return (
                f"{rival_text(rival)} has priority {other.priority}, {gap} above this workload's {workload.priority}."
            )
        if standing.kind == StandingKind.FOCUS:
            return f'{rival_text(rival)} is {other.dev_engine} work, which the worker prefers.'
        return (
            f'{rival_text(rival)} has {plural(rival.threads or 0, "thread")} for throughput {other.throughput}; '
            f'this workload has {plural(standing.threads, "thread")} for throughput {workload.throughput}.'
        )

    kind = {
        StandingKind.OUTRANKED: EvidenceKind.HIGHER_PRIORITY,
        StandingKind.FOCUS: EvidenceKind.FOCUS,
    }.get(standing.kind, EvidenceKind.THROUGHPUT_SHARE)
    return [Evidence(kind, text(rival), workload_link(rival.workload)) for rival in standing.rivals[:SHOWN_RIVALS]]


def diagnose_passed_over(workload: Test, standing: Standing, groups: Sequence[WorkerGroup], now: datetime) -> Diagnosis:

    evidence = [*rival_evidence(workload, standing), *eligible_evidence(groups, now)]
    count = len(standing.rivals)
    rivals = plural(count, 'workload')
    single = count == 1

    if standing.kind == StandingKind.OUTRANKED:
        top = standing.rivals[0].workload.priority
        return verdict(
            DiagnosisState.OUTRANKED,
            f'Outranked: workers take the highest priority first, and {rivals} at priority {top} '
            f'{"is" if single else "are"} ahead of this one at priority {workload.priority}.',
            f'outranked by priority {top}',
            evidence,
        )

    if standing.kind == StandingKind.FOCUS:
        engine = standing.rivals[0].workload.dev_engine
        return verdict(
            DiagnosisState.OUTRANKED,
            f'Outranked: the workers that can take it run with --focus or --only for {engine}, '
            f'and {rivals} of that engine {"is" if single else "are"} waiting.',
            f'outranked by {engine} focus',
            evidence,
        )

    return verdict(
        DiagnosisState.LOW_SHARE,
        f'Low share: {rivals} {"has" if single else "have"} fewer threads for the throughput setting, so the next '
        'worker goes there first.',
        'low throughput share',
        evidence,
    )


def diagnose_next_in_line(groups: Sequence[WorkerGroup], errors: Sequence[LogEvent], now: datetime) -> Diagnosis:

    if errors:
        failed = plural(len({event.machine_id for event in errors}), 'worker')
        newest = errors[0]
        return verdict(
            DiagnosisState.FAILING,
            f'Failing: the last {failed} to take it reported an error and no result has arrived since, '
            f'most recently "{newest.summary}" {ago(now, newest.created)}.',
            f'failing: {newest.summary}',
            [*error_evidence(errors, now), *eligible_evidence(groups, now)],
        )

    return verdict(
        DiagnosisState.WAITING,
        f'Queued: a worker that can take it was seen {ago(now, groups[0].last_seen)} and this workload is next '
        f'in line. {BUSY_NOTE}',
        'next in line for a worker',
        [*eligible_evidence(groups, now), Evidence(EvidenceKind.LIMIT, VISIBILITY_LIMIT)],
    )


def best_standing(standings: Sequence[Standing]) -> Standing:
    order = (StandingKind.NEXT, StandingKind.OUTRANKED, StandingKind.FOCUS, StandingKind.LOW_SHARE)
    return min(standings, key=lambda standing: order.index(standing.kind))


def diagnose_unassigned(workload: Test, judge: FleetJudge, activity: Activity, now: datetime) -> Diagnosis:

    fleet = judge.fleet
    errors = unresolved_errors(activity)
    judged = [(group, judge.obstacles(workload, group.machine)) for group in fleet.groups]
    eligible = [group for group, found in judged if not found]
    blocked = [(group, found) for group, found in judged if found]
    seen = [group for group in fleet.groups if group.recent(now)]
    ready = [group for group in eligible if group.recent(now)]

    if not seen and (eligible or not blocked):
        return diagnose_no_workers(fleet.last_seen, eligible, errors, now)

    if not ready:
        recent_blocked = [(group, found) for group, found in blocked if group.recent(now)]
        return diagnose_no_eligible(recent_blocked if eligible else blocked, eligible, errors, now)

    standing = best_standing([judge.standing(workload, group.machine) for group in ready])
    if standing.kind == StandingKind.NEXT:
        return diagnose_next_in_line(ready, errors, now)
    return diagnose_passed_over(workload, standing, ready, now)


def diagnose_active(workload: Test, judge: FleetJudge, activity: Activity, now: datetime) -> Diagnosis:

    holders = [machine for machine in judge.fleet.assigned if machine.workload == workload.id]
    if holders:
        return diagnose_held(workload, holders, activity, now)

    return diagnose_starting(activity, unresolved_errors(activity), now) or diagnose_unassigned(
        workload, judge, activity, now
    )


def diagnose(workload: Test, judge: FleetJudge, activity: Activity, now: datetime) -> Diagnosis:

    if workload.finished or workload.deleted:
        return diagnose_finished(workload, activity, now)
    if not workload.approved:
        return diagnose_pending()
    return diagnose_active(workload, judge, activity, now)
