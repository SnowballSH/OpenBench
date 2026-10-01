from collections.abc import Iterator, Sequence
from datetime import UTC, datetime

from OpenBench.digest.domain import (
    DigestWindow,
    ErrorDigest,
    FinishedCounts,
    FinishedDigest,
    FleetActivity,
    RunningDigest,
    TrunkMovement,
)
from OpenBench.insights.listing import format_duration
from OpenBench.progress.present import core_hours_text, count, elo_text, plural


def utc_moment(moment: datetime) -> str:
    at = moment.astimezone(UTC)
    return f'{at:%b} {at.day}, {at:%H:%M} UTC'


def window_phrase(window: DigestWindow) -> str:
    if window.preset is not None:
        return f'in the last {window.preset.label}'
    return f'since {utc_moment(window.since)}'


def span_phrase(window: DigestWindow) -> str:
    if window.preset is not None:
        return f'the last {window.preset.label}'
    return f'the {format_duration(window.span.total_seconds())} since {utc_moment(window.since)}'


def outcome_phrase(counts: FinishedCounts) -> str:
    parts = [
        f'{count(counts.passed)} passed' if counts.passed else '',
        f'{count(counts.failed)} failed' if counts.failed else '',
        f'{count(counts.undecided)} without a verdict' if counts.undecided else '',
    ]
    return ', '.join(part for part in parts if part)


def finished_phrase(counts: FinishedCounts, window: DigestWindow) -> str:
    if not counts.total:
        return f'Nothing finished {window_phrase(window)}'
    return f'{plural(counts.total, "workload")} finished {window_phrase(window)} ({outcome_phrase(counts)})'


def trunk_phrases(trunk: Sequence[TrunkMovement]) -> Iterator[str]:
    several = len(trunk) > 1
    for movement in trunk:
        owner = f'{movement.engine} ' if several else ''
        for found in movement.classes:
            if found.net is not None:
                yield f'{elo_text(found.net, 1)} Elo chained on the {owner}{found.time_class.label} trunk'


def running_phrases(running: RunningDigest) -> Iterator[str]:
    active = running.total - running.pending
    if active:
        yield f'{count(active)} still running'
    if running.pending:
        yield f'{count(running.pending)} awaiting approval'


def error_phrases(errors: ErrorDigest) -> Iterator[str]:
    if errors.unresolved:
        yield f'{plural(errors.unresolved, "unresolved error group")}'
    elif errors.total:
        yield f'{plural(errors.total, "error group")}, all resolved'


def fleet_sentence(fleet: FleetActivity, window: DigestWindow) -> str:
    if not fleet.games:
        return f'The fleet played no games in {span_phrase(window)}.'
    hosts = f' on {plural(fleet.hosts, "host")}' if fleet.hosts else ''
    hours = ''
    if fleet.core_hours is not None:
        hours = f', {"about " if fleet.core_hours_estimated else ""}{core_hours_text(fleet.core_hours)} of search'
    return f'The fleet played {plural(fleet.games, "game")}{hosts}{hours}.'


def headline(
    window: DigestWindow,
    finished: FinishedDigest,
    running: RunningDigest,
    trunk: Sequence[TrunkMovement],
    fleet: FleetActivity,
    errors: ErrorDigest,
) -> list[str]:
    phrases = [
        finished_phrase(finished.counts, window),
        *trunk_phrases(trunk),
        *running_phrases(running),
        *error_phrases(errors),
    ]
    return [f'{", ".join(phrases)}.', fleet_sentence(fleet, window)]
