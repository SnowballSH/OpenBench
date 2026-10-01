import re
from dataclasses import dataclass
from datetime import datetime

from OpenBench.fleet.status import relative_age
from OpenBench.models import Test
from OpenBench.page_queries import is_listed

SHORT_SHA_LENGTH = 8

# Seven to forty hex digits with at least one decimal digit, so "deadbeef" and "defaced" stay branch names
COMMIT_NAME = re.compile(r'(?=[a-f]*[0-9])[0-9a-f]{7,40}', re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class WorkloadLabel:
    title: str
    commits: str | None
    repeats_info: bool


@dataclass(frozen=True, slots=True)
class RowMoment:
    verb: str
    at: datetime
    ago: str


def is_commit_name(name: str) -> bool:
    return COMMIT_NAME.fullmatch(name) is not None


def short_name(name: str) -> str:
    return name[:SHORT_SHA_LENGTH].lower() if is_commit_name(name) else name


def info_subject(info: str) -> str:
    lines = info.strip().splitlines()
    return lines[0].strip() if lines else ''


def commit_pair(test: Test) -> str:
    dev, base = test.dev.name, test.base.name
    return short_name(dev) if dev == base else f'{short_name(dev)} vs {short_name(base)}'


def workload_label(test: Test, pretty_name: str) -> WorkloadLabel:

    # pretty_name is prettyDevName's answer; it stops being the commit when a network or another engine names the row
    name = test.dev.name
    if not (is_commit_name(name) and name.lower().startswith(pretty_name.lower())):
        return WorkloadLabel(title=pretty_name, commits=None, repeats_info=False)

    subject = info_subject(test.info)
    if not subject:
        return WorkloadLabel(title=commit_pair(test), commits=None, repeats_info=False)

    return WorkloadLabel(title=subject, commits=commit_pair(test), repeats_info=subject == test.info.strip())


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
