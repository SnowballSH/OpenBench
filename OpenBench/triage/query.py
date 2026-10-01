from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Self
from urllib.parse import urlencode

from django.http import QueryDict

from OpenBench.triage.kinds import GAME, ErrorKind, parse_kinds

MAX_ID_DIGITS = 18
TRUE_VALUES = frozenset({'1', 'true', 'on', 'yes'})
DEFAULT_LIMIT = 25
MAX_LIMIT = 100


class View(StrEnum):
    GROUPS = 'groups'
    LIST = 'list'


@dataclass(frozen=True, slots=True)
class KindOption:
    value: str
    label: str


KIND_OPTIONS = (
    KindOption('', 'All'),
    KindOption(ErrorKind.BUILD.value, ErrorKind.BUILD.label),
    KindOption(ErrorKind.BENCH.value, ErrorKind.BENCH.label),
    KindOption(GAME, 'Game'),
    KindOption(ErrorKind.CRASH.value, ErrorKind.CRASH.label),
    KindOption(ErrorKind.TIME_LOSS.value, ErrorKind.TIME_LOSS.label),
    KindOption(ErrorKind.ILLEGAL.value, ErrorKind.ILLEGAL.label),
    KindOption(ErrorKind.GENFENS.value, ErrorKind.GENFENS.label),
    KindOption(ErrorKind.OTHER.value, ErrorKind.OTHER.label),
)


def parse_id(value: str | None) -> int | None:
    text = (value or '').strip().removeprefix('#')
    return int(text) if text.isascii() and text.isdigit() and len(text) <= MAX_ID_DIGITS else None


def parse_limit(value: str | None) -> int:
    limit = parse_id(value)
    return DEFAULT_LIMIT if limit is None else max(1, min(limit, MAX_LIMIT))


@dataclass(frozen=True, slots=True)
class ErrorQuery:
    workload: int | None = None
    kind: str = ''
    unresolved: bool = False
    view: View = View.GROUPS

    @classmethod
    def parse(cls, params: QueryDict) -> Self:
        kind = params.get('kind', '')
        return cls(
            workload=parse_id(params.get('workload')),
            kind=kind if parse_kinds(kind) else '',
            unresolved=params.get('unresolved', '').lower() in TRUE_VALUES,
            view=View.LIST if params.get('view') == View.LIST else View.GROUPS,
        )

    @property
    def kinds(self) -> frozenset[ErrorKind] | None:
        return parse_kinds(self.kind)

    @property
    def filtered(self) -> bool:
        return self.workload is not None or bool(self.kind) or self.unresolved

    def with_kind(self, kind: str) -> Self:
        return replace(self, kind=kind)

    def with_view(self, view: View) -> Self:
        return replace(self, view=view)

    @property
    def toggled_unresolved(self) -> Self:
        return replace(self, unresolved=not self.unresolved)

    @property
    def without_workload(self) -> Self:
        return replace(self, workload=None)

    @property
    def as_groups(self) -> Self:
        return self.with_view(View.GROUPS)

    @property
    def as_list(self) -> Self:
        return self.with_view(View.LIST)

    @property
    def is_list(self) -> bool:
        return self.view is View.LIST

    @property
    def kind_links(self) -> list[tuple[KindOption, str, bool]]:
        return [
            (option, self.with_kind(option.value).querystring, option.value == self.kind) for option in KIND_OPTIONS
        ]

    @property
    def querystring(self) -> str:
        pairs = {
            'workload': '' if self.workload is None else str(self.workload),
            'kind': self.kind,
            'unresolved': '1' if self.unresolved else '',
            'view': View.LIST.value if self.is_list else '',
        }
        encoded = urlencode({name: value for name, value in pairs.items() if value})
        return f'?{encoded}' if encoded else ''
