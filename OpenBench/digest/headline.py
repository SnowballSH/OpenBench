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
    outcomes = (
        (counts.passed, 'passed'),
        (counts.failed, 'failed'),
        (counts.completed, 'completed'),
        (counts.stopped, 'stopped'),
    )
    return ', '.join(f'{count(total)} {word}' for total, word in outcomes if total)


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
                again = f' ({count(found.remeasured)} re-measured)' if found.remeasured else ''
                yield f'{elo_text(found.net, 1)} Elo chained on the {owner}{found.time_class.label} trunk{again}'


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


def hours_phrase(fleet: FleetActivity) -> str:
    if fleet.core_hours is None:
        return ''
    bound = 'at least ' if fleet.games_without_hours else 'about ' if fleet.core_hours_estimated else ''
    return f', {bound}{core_hours_text(fleet.core_hours)} of search'


def fleet_sentence(fleet: FleetActivity, window: DigestWindow) -> str:
    if not fleet.games:
        return f'The fleet played no games in {span_phrase(window)}.'
    pools = len(fleet.pools) + fleet.pools_omitted
    where = f' on {plural(pools, "pool")}' if pools else ''
    return f'The fleet played {plural(fleet.games, "game")}{where}{hours_phrase(fleet)}.'


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
