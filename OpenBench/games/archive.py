import bz2
import os
import tarfile
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from OpenBench.games.aggregate import Aggregate, PairTracker
from OpenBench.games.domain import Adjudication
from OpenBench.games.facts import game_facts
from OpenBench.games.pgn import parse_games

MAX_MEMBER_CHARS = 8 << 20
MAX_LINE_CHARS = 1 << 20
DEADLINE_CHECK_LINES = 64
TAIL_SETTLE_SECONDS = 30.0
DAMAGED_STREAM = (OSError, EOFError, ValueError)


@dataclass(frozen=True, slots=True)
class Budget:
    compressed_bytes: int
    games: int
    seconds: float


@dataclass(frozen=True, slots=True)
class Member:
    size: int
    next_offset: int
    stream: IO[bytes] | None


@dataclass(frozen=True, slots=True)
class Progress:
    offset: int
    members: int
    reached_end: bool
    misaligned: bool = False


def archive_members(handle: IO[bytes], offset: int, archive_bytes: int) -> Iterator[Member]:
    """Yields the complete tar members from a byte offset; stops at a member still being appended."""

    handle.seek(offset)
    try:
        with tarfile.open(fileobj=handle, mode='r:') as tar:
            while (info := tar.next()) is not None and info.offset_data + info.size <= archive_bytes:
                yield Member(info.size, tar.offset, tar.extractfile(info) if info.isfile() else None)
    except tarfile.TarError:
        return


def bounded_lines(stream: IO[bytes], deadline: float) -> Iterator[str]:
    chars = 0
    with bz2.open(stream, 'rt', encoding='utf-8', errors='replace') as text:
        for count, line in enumerate(iter(lambda: text.readline(MAX_LINE_CHARS), '')):
            chars += len(line)
            if chars > MAX_MEMBER_CHARS:
                raise ValueError('PGN batch exceeds the decompressed size limit')
            if count % DEADLINE_CHECK_LINES == 0 and time.monotonic() >= deadline:
                raise TimeoutError('PGN batch exceeds the time allowed for one batch')
            yield line


def analyse_member(stream: IO[bytes], aggregate: Aggregate, rules: Adjudication, deadline: float) -> None:

    pairs = PairTracker(aggregate)
    try:
        for game in parse_games(bounded_lines(stream, deadline)):
            facts = game_facts(game, rules) if game else None
            if facts is None:
                aggregate.add_malformed()
            else:
                aggregate.add_game(facts)
                pairs.add(facts)
    except DAMAGED_STREAM:
        aggregate.add_damaged_member()
    finally:
        pairs.close()


def member_delta(member: Member, rules: Adjudication, budget: Budget, deadline: float) -> Aggregate:
    delta = Aggregate()
    if member.stream is not None:
        analyse_member(member.stream, delta, rules, max(deadline, time.monotonic() + budget.seconds))
    return delta


def settled(handle: IO[bytes]) -> bool:
    return time.time() - os.fstat(handle.fileno()).st_mtime >= TAIL_SETTLE_SECONDS


def unreadable_block(handle: IO[bytes], offset: int) -> bool:
    handle.seek(offset)
    block = handle.read(tarfile.BLOCKSIZE)
    if len(block) < tarfile.BLOCKSIZE or not block.strip(b'\0'):
        return False
    try:
        tarfile.TarInfo.frombuf(block, tarfile.ENCODING, 'surrogateescape')
    except tarfile.HeaderError:
        return True
    return False


def on_member_boundary(path: Path, offset: int) -> bool:
    """Whether a walk over the archive's headers from its start lands exactly on the offset."""

    try:
        with tarfile.open(path, 'r:') as tar:
            while tar.offset < offset and tar.next() is not None:
                pass
            return tar.offset == offset
    except tarfile.TarError:
        return False


def analyse_archive(path: Path, offset: int, aggregate: Aggregate, rules: Adjudication, budget: Budget) -> Progress:
    """Folds whole members into the aggregate until the archive or the budget runs out.

    A damaged member at the end of the archive may be one the watcher is still writing into the
    old padding, so it is held back, and read again next pass, until the archive has settled.
    """

    deadline = time.monotonic() + budget.seconds
    games_before = aggregate.totals['games']
    spent = members = 0
    held: tuple[Aggregate, Member] | None = None

    def fold(delta: Aggregate, member: Member) -> None:
        nonlocal offset, spent, members
        aggregate.merge(delta)
        offset, spent, members = member.next_offset, spent + member.size, members + 1

    with path.open('rb') as handle:
        archive_bytes = os.fstat(handle.fileno()).st_size
        for member in archive_members(handle, offset, archive_bytes):
            if held:
                fold(*held)
                held = None

            exhausted = (
                spent >= budget.compressed_bytes
                or aggregate.totals['games'] - games_before >= budget.games
                or time.monotonic() >= deadline
            )
            if members and exhausted:
                return Progress(offset, members, reached_end=False)

            delta = member_delta(member, rules, budget, deadline)
            if delta.totals['damaged_members']:
                held = (delta, member)
            else:
                fold(delta, member)

        if held and settled(handle):
            fold(*held)
        misaligned = not members and not held and settled(handle) and unreadable_block(handle, offset)

    return Progress(offset, members, reached_end=True, misaligned=misaligned)
