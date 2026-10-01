import re
from dataclasses import dataclass
from enum import StrEnum
from functools import reduce
from operator import or_

from django.db.models import Q

from OpenBench.workload_names import short_name

BUILD_SUFFIX = ' build failed'
WRONG_BENCH = 'Wrong Bench'
BENCH_FAILURES = ('Bench Exceeded Max Duration', 'Failed to Execute Benchmark', 'Non-Deterministic Benches')
GENFENS_STALL = 'Stalled during genfens'
CRASHES = ('Disconnect', 'Stalled')
ILLEGAL_MOVE = 'Illegal Move'
TIME_LOSSES = ('Time Loss', 'Timeloss')

BRACKETED = re.compile(r'\[(?P<subject>[^\]]*)\] (?P<rest>.*)', re.DOTALL)
BUILD = re.compile(r'\[(?P<engine>[^\]]*)\] (?P<branch>.*) build failed', re.DOTALL)
WRONG_BENCH_VALUE = re.compile(rf'{WRONG_BENCH}: (?P<bench>-?[0-9]{{1,18}})')


class ErrorKind(StrEnum):
    BUILD = 'build'
    BENCH = 'bench'
    CRASH = 'crash'
    TIME_LOSS = 'timeloss'
    ILLEGAL = 'illegal'
    GENFENS = 'genfens'
    OTHER = 'other'

    @property
    def label(self) -> str:
        return KIND_LABELS[self]


KIND_LABELS = {
    ErrorKind.BUILD: 'Build',
    ErrorKind.BENCH: 'Bench',
    ErrorKind.CRASH: 'Crash',
    ErrorKind.TIME_LOSS: 'Time loss',
    ErrorKind.ILLEGAL: 'Illegal move',
    ErrorKind.GENFENS: 'Genfens',
    ErrorKind.OTHER: 'Other',
}

GAME = 'game'
GAME_KINDS = frozenset({ErrorKind.CRASH, ErrorKind.TIME_LOSS, ErrorKind.ILLEGAL})


@dataclass(frozen=True, slots=True)
class Signature:
    kind: ErrorKind
    title: str
    subject: str = ''
    bench: int | None = None

    @property
    def key(self) -> tuple[str, str, str]:
        return self.kind.value, self.title, self.subject


def bracketed_signature(subject: str, rest: str) -> Signature:

    if wrong := WRONG_BENCH_VALUE.fullmatch(rest):
        return Signature(ErrorKind.BENCH, WRONG_BENCH, subject, int(wrong['bench']))

    if rest in BENCH_FAILURES:
        return Signature(ErrorKind.BENCH, rest, subject)

    if rest == GENFENS_STALL:
        return Signature(ErrorKind.GENFENS, rest, subject)

    return Signature(ErrorKind.OTHER, rest, subject)


def signature(summary: str) -> Signature:

    if build := BUILD.fullmatch(summary):
        return Signature(ErrorKind.BUILD, f'{build["engine"]}{BUILD_SUFFIX}', short_name(build['branch']))

    if match := BRACKETED.fullmatch(summary):
        return bracketed_signature(match['subject'], match['rest'])

    if summary in CRASHES:
        return Signature(ErrorKind.CRASH, summary)

    if summary == ILLEGAL_MOVE:
        return Signature(ErrorKind.ILLEGAL, summary)

    if summary.lower() in {name.lower() for name in TIME_LOSSES}:
        return Signature(ErrorKind.TIME_LOSS, TIME_LOSSES[0])

    return Signature(ErrorKind.OTHER, summary)


def any_of(conditions: list[Q]) -> Q:
    return reduce(or_, conditions)


def bracketed(rest: str) -> Q:
    return Q(summary__regex=rf'\A\[[^\]]*\] {rest}\Z')


# The same rules as signature(), for the database; test_triage.py holds the two together
KIND_CONDITIONS: dict[ErrorKind, Q] = {
    ErrorKind.BUILD: bracketed(rf'(.|\n)*{BUILD_SUFFIX}'),
    ErrorKind.BENCH: any_of(
        [bracketed(rf'{WRONG_BENCH}: -?[0-9]{{1,18}}'), *(bracketed(re.escape(name)) for name in BENCH_FAILURES)]
    ),
    ErrorKind.CRASH: Q(summary__in=CRASHES),
    ErrorKind.TIME_LOSS: any_of([Q(summary__iexact=name) for name in TIME_LOSSES]),
    ErrorKind.ILLEGAL: Q(summary=ILLEGAL_MOVE),
    ErrorKind.GENFENS: bracketed(re.escape(GENFENS_STALL)),
}


def kind_condition(kinds: frozenset[ErrorKind]) -> Q:
    known = [KIND_CONDITIONS[kind] for kind in kinds if kind is not ErrorKind.OTHER]
    if ErrorKind.OTHER not in kinds:
        return any_of(known)
    excluded = [condition for kind, condition in KIND_CONDITIONS.items() if kind not in kinds]
    return ~any_of(excluded) if excluded else Q()


def parse_kinds(value: str | None) -> frozenset[ErrorKind] | None:
    if value == GAME:
        return GAME_KINDS
    try:
        return frozenset({ErrorKind(value or '')})
    except ValueError:
        return None
