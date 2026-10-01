from dataclasses import dataclass
from datetime import datetime

from OpenBench.fleet.status import relative_age
from OpenBench.models import Test
from OpenBench.page_queries import is_listed
from OpenBench.workload_names import commit_pair, is_commit_name, split_info


@dataclass(frozen=True, slots=True)
class WorkloadLabel:
    title: str
    commits: str | None
    info: str


@dataclass(frozen=True, slots=True)
class RowMoment:
    verb: str
    at: datetime
    ago: str


RESULT_LABELS = {
    'green': 'Passed',
    'blue': 'Passed, non-regression',
    'yellow': 'Failed, wins at least losses',
    'red': 'Failed',
}


def result_label(test: Test, colour: str) -> str:
    if colour in RESULT_LABELS:
        return RESULT_LABELS[colour]
    if test.finished:
        return 'Finished'
    return 'Running' if test.approved else 'Pending approval'


def workload_label(test: Test, pretty_name: str) -> WorkloadLabel:

    # pretty_name is prettyDevName's answer; it stops being the commit when a network or another engine names the row
    name = test.dev.name
    if not (is_commit_name(name) and name.lower().startswith(pretty_name.lower())):
        return WorkloadLabel(title=pretty_name, commits=None, info=test.info)

    subject, rest = split_info(test.info)
    if not subject:
        return WorkloadLabel(title=commit_pair(test), commits=None, info='')

    return WorkloadLabel(title=subject, commits=commit_pair(test), info=rest)


def row_moment(verb: str, at: datetime, now: datetime) -> RowMoment:
    return RowMoment(verb=verb, at=at, ago=relative_age(now - at))


def listing_moment(test: Test) -> RowMoment | None:

    if not is_listed(test):
        return None

    now = test.listing_as_of
    if test.finished:
        return row_moment('finished', test.finished_at, now)

    if test.approved and test.first_snapshot_at is not None:
        return row_moment('started', test.first_snapshot_at, now)

    return row_moment('created', test.creation, now)
