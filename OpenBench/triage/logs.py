import os
import re
from dataclasses import dataclass
from pathlib import Path

from django.conf import settings

from OpenBench.models import LogEvent

MAX_LOG_BYTES = 2 * 1024 * 1024
VISIBLE_EDGE_LINES = 120
MAX_FOLDED_LINES = 5000
MAX_KEY_LINES = 30
KEY_LINE_LENGTH = 240
TRACEBACK_CONTEXT_LINES = 3

ANSI_SEQUENCE = re.compile(r'\x1b\[[0-9;?]*[ -/]*[@-~]')
# Control characters, and the bidirectional overrides that can reorder what a reader sees
UNSAFE_CHARACTERS = re.compile(r'[\x00-\x08\x0b-\x1f\x7f‪-‮⁦-⁩]')
REPLACEMENT = '�'

COMPILER_ERROR = re.compile(
    r'\berror\b[:\[]|\bfatal error\b|undefined reference|symbol\(s\) not found|\*\*\* \[.*\] Error [0-9]+'
)
TRACEBACK = 'Traceback (most recent call last):'
ILLEGAL_MOVE = re.compile(r'illegal move', re.IGNORECASE)
TERMINATION = re.compile(r'\[Termination "')


@dataclass(frozen=True, slots=True)
class LogText:
    text: str
    size: int
    limit: int

    @property
    def truncated(self) -> bool:
        return self.size > self.limit


@dataclass(frozen=True, slots=True)
class LogLine:
    number: int
    text: str


@dataclass(frozen=True, slots=True)
class KeyLine:
    number: int
    text: str
    label: str
    shown: bool


@dataclass(frozen=True, slots=True)
class LogExcerpt:
    head: tuple[LogLine, ...]
    folded: tuple[LogLine, ...]
    tail: tuple[LogLine, ...]
    omitted: int
    total: int

    @property
    def shown_numbers(self) -> frozenset[int]:
        return frozenset(line.number for line in (*self.head, *self.folded, *self.tail))


def log_name(event_id: int) -> str:
    return f'event{event_id}.log'


def has_log(event: LogEvent) -> bool:
    return event.log_file == log_name(event.id)


def log_path(event: LogEvent) -> Path | None:

    # The name is rebuilt from the id, so nothing stored on the row can point outside the media directory
    if not has_log(event):
        return None

    path = Path(settings.MEDIA_ROOT) / log_name(event.id)
    return path if path.is_file() and not path.is_symlink() else None


def read_log(path: Path, limit: int | None = None) -> LogText:
    limit = limit or MAX_LOG_BYTES
    with path.open('rb') as log:
        size = os.fstat(log.fileno()).st_size
        data = log.read(limit)
    return LogText(text=data.decode('utf-8', errors='replace'), size=size, limit=limit)


def displayable(line: str) -> str:
    return UNSAFE_CHARACTERS.sub(REPLACEMENT, ANSI_SEQUENCE.sub('', line))


def log_lines(text: str) -> list[LogLine]:
    return [LogLine(number, displayable(line)) for number, line in enumerate(text.splitlines(), start=1)]


def excerpt(lines: list[LogLine]) -> LogExcerpt:

    total = len(lines)
    if total <= 2 * VISIBLE_EDGE_LINES:
        return LogExcerpt(head=tuple(lines), folded=(), tail=(), omitted=0, total=total)

    middle = lines[VISIBLE_EDGE_LINES:-VISIBLE_EDGE_LINES]
    return LogExcerpt(
        head=tuple(lines[:VISIBLE_EDGE_LINES]),
        folded=tuple(middle[:MAX_FOLDED_LINES]),
        tail=tuple(lines[-VISIBLE_EDGE_LINES:]),
        omitted=max(0, len(middle) - MAX_FOLDED_LINES),
        total=total,
    )


def traceback_lines(lines: list[LogLine], start: int) -> list[LogLine]:

    following = lines[start + 1 :]
    context = following[:TRACEBACK_CONTEXT_LINES]
    raised = next((line for line in following if line.text and not line.text[0].isspace()), None)
    return [lines[start], *context, *([raised] if raised and raised not in context else [])]


def labelled_lines(lines: list[LogLine]) -> dict[int, tuple[LogLine, str]]:

    found: dict[int, tuple[LogLine, str]] = {}

    def keep(line: LogLine, label: str) -> None:
        found.setdefault(line.number, (line, label))

    for index, line in enumerate(lines):
        if line.text.strip() == TRACEBACK:
            for part in traceback_lines(lines, index):
                keep(part, 'traceback')
        elif COMPILER_ERROR.search(line.text):
            keep(line, 'error')
        elif ILLEGAL_MOVE.search(line.text):
            keep(line, 'illegal move')
        elif TERMINATION.search(line.text):
            keep(line, 'termination')

    return found


def key_lines(lines: list[LogLine], shown: frozenset[int]) -> tuple[KeyLine, ...]:
    found = labelled_lines(lines)
    return tuple(
        KeyLine(number=number, text=line.text.strip()[:KEY_LINE_LENGTH], label=label, shown=number in shown)
        for number, (line, label) in sorted(found.items())[:MAX_KEY_LINES]
    )
