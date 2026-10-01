import logging
from pathlib import Path

from django.core.files.storage import FileSystemStorage
from django.db import IntegrityError, transaction
from django.utils import timezone

from OpenBench.games.aggregate import STATE_VERSION, Aggregate
from OpenBench.games.archive import Budget, analyse_archive, on_member_boundary
from OpenBench.games.domain import adjudication_of
from OpenBench.models import GameAnalysis, Test

LOGGER = logging.getLogger(__name__)

WATCHER_BUDGET = Budget(compressed_bytes=4 << 20, games=20_000, seconds=2.0)
VIEW_BUDGET = Budget(compressed_bytes=2 << 20, games=10_000, seconds=1.0)


def archive_path(test_id: int) -> Path:
    return Path(FileSystemStorage().path(f'PGNs/{test_id}.pgn.tar'))


def archive_size(test_id: int) -> int | None:
    try:
        return archive_path(test_id).stat().st_size
    except OSError:
        return None


def analysis_row(test: Test) -> GameAnalysis:
    if row := GameAnalysis.objects.filter(test=test).first():
        return row
    try:
        with transaction.atomic():
            return GameAnalysis.objects.create(test=test)
    except IntegrityError:
        return GameAnalysis.objects.get(test=test)


def usable(row: GameAnalysis, archive_bytes: int) -> bool:
    return row.version == STATE_VERSION and row.analysed_bytes <= archive_bytes


def refresh(test: Test, budget: Budget) -> GameAnalysis | None:
    """Folds archive members not yet analysed into the stored aggregate, within the budget."""

    if (archive_bytes := archive_size(test.id)) is None:
        return GameAnalysis.objects.filter(test=test, version=STATE_VERSION).first()

    row = analysis_row(test)
    resumed = usable(row, archive_bytes)
    aggregate = Aggregate.from_state(row.state) if resumed else Aggregate()
    offset = row.analysed_bytes if resumed else 0

    path, rules = archive_path(test.id), adjudication_of(test.win_adj, test.draw_adj)
    progress = analyse_archive(path, offset, aggregate, rules, budget)
    if progress.misaligned and offset and not on_member_boundary(path, offset):
        resumed, aggregate = False, Aggregate()
        progress = analyse_archive(path, 0, aggregate, rules, budget)

    if resumed and not progress.members and row.complete == progress.reached_end:
        return row

    claimed = GameAnalysis.objects.filter(
        pk=row.pk, version=row.version, analysed_bytes=row.analysed_bytes, members=row.members
    ).update(
        version=STATE_VERSION,
        state=aggregate.to_state(),
        games=aggregate.totals['games'],
        members=(row.members if resumed else 0) + progress.members,
        analysed_bytes=progress.offset,
        complete=progress.reached_end,
        updated=timezone.now(),
    )
    if not claimed:
        LOGGER.info('Another pass analysed the games of Workload #%d first', test.id)

    return GameAnalysis.objects.get(pk=row.pk)


def refresh_after_archiving(test_id: int) -> None:
    """Called by the PGN watcher; whatever goes wrong here must never stop it archiving."""

    try:
        if test := Test.objects.filter(id=test_id).first():
            refresh(test, WATCHER_BUDGET)
    except Exception:
        LOGGER.exception('Could not analyse the archived games of Workload #%d', test_id)
