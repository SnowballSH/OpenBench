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

MAX_MEMBER_CHARS = 64 << 20
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


def archive_members(handle: IO[bytes], offset: int, archive_bytes: int) -> Iterator[Member]:
    """Yields the complete tar members from a byte offset; stops at a member still being appended."""

    handle.seek(offset)
    try:
        with tarfile.open(fileobj=handle, mode='r:') as tar:
            while (info := tar.next()) is not None and info.offset_data + info.size <= archive_bytes:
                yield Member(info.size, tar.offset, tar.extractfile(info) if info.isfile() else None)
    except tarfile.TarError:
        return


def bounded_lines(stream: IO[bytes]) -> Iterator[str]:
    chars = 0
    with bz2.open(stream, 'rt', encoding='utf-8', errors='replace') as text:
        for line in text:
            chars += len(line)
            if chars > MAX_MEMBER_CHARS:
                raise ValueError('PGN batch exceeds the decompressed size limit')
            yield line


def analyse_member(stream: IO[bytes], aggregate: Aggregate, rules: Adjudication) -> None:

    pairs = PairTracker(aggregate)
    try:
        for game in parse_games(bounded_lines(stream)):
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


def analyse_archive(path: Path, offset: int, aggregate: Aggregate, rules: Adjudication, budget: Budget) -> Progress:
    """Folds whole members into the aggregate until the archive or the budget runs out."""

    deadline = time.monotonic() + budget.seconds
    games_before = aggregate.totals['games']
    spent = members = 0

    with path.open('rb') as handle:
        archive_bytes = os.fstat(handle.fileno()).st_size
        for member in archive_members(handle, offset, archive_bytes):
            exhausted = (
                spent >= budget.compressed_bytes
                or aggregate.totals['games'] - games_before >= budget.games
                or time.monotonic() >= deadline
            )
            if members and exhausted:
                return Progress(offset, members, reached_end=False)

            if member.stream is not None:
                analyse_member(member.stream, aggregate, rules)
            offset, spent, members = member.next_offset, spent + member.size, members + 1

    return Progress(offset, members, reached_end=True)
