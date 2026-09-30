import os
import shutil
from collections.abc import Iterator
from pathlib import Path

from OpenBench.storage.domain import (
    DatabaseUsage,
    DirectoryUsage,
    DiskUsage,
    MediaFile,
    MediaScan,
)

PGN_ARCHIVE_DIR = "PGNs"
MAX_ENTRIES = 100_000


class ScanBudget:
    def __init__(self, entries: int = MAX_ENTRIES) -> None:
        self.remaining = entries
        self.exhausted = False

    def take(self) -> bool:
        if self.remaining <= 0:
            self.exhausted = True
            return False
        self.remaining -= 1
        return True


def file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def entry_size(entry: os.DirEntry[str]) -> int | None:
    try:
        return entry.stat(follow_symlinks=False).st_size
    except OSError:
        return None


def list_entries(directory: Path | str) -> list[os.DirEntry[str]]:
    try:
        with os.scandir(directory) as entries:
            return list(entries)
    except OSError:
        return []


def disk_usage(path: Path) -> DiskUsage | None:
    for candidate in (path, *path.parents):
        try:
            usage = shutil.disk_usage(candidate)
        except OSError:
            continue
        return DiskUsage(total=usage.total, used=usage.used, free=usage.free)
    return None


def database_usage(path: Path) -> DatabaseUsage:
    return DatabaseUsage(
        main=file_size(path),
        wal=file_size(path.with_name(path.name + "-wal")),
        shm=file_size(path.with_name(path.name + "-shm")),
    )


def walk_files(directory: Path | str, budget: ScanBudget) -> Iterator[int]:
    pending = [directory]
    while pending:
        for entry in list_entries(pending.pop()):
            if not budget.take():
                return
            if entry.is_symlink():
                continue
            if entry.is_dir(follow_symlinks=False):
                pending.append(entry.path)
            elif (size := entry_size(entry)) is not None:
                yield size


def directory_usage(directory: Path, budget: ScanBudget) -> DirectoryUsage:
    sizes = list(walk_files(directory, budget))
    return DirectoryUsage(files=len(sizes), size=sum(sizes), truncated=budget.exhausted)


class MediaScanner:
    def __init__(self, root: Path, budget: ScanBudget) -> None:
        self.root = root
        self.budget = budget
        self.files: list[MediaFile] = []
        self.skipped_symlinks = 0

    def scan(self) -> MediaScan:
        self.scan_level(self.root, prefix="", archive_level=False)
        return MediaScan(
            files=tuple(self.files),
            skipped_symlinks=self.skipped_symlinks,
            truncated=self.budget.exhausted,
        )

    def scan_level(
        self, directory: Path | str, prefix: str, archive_level: bool
    ) -> None:
        for entry in list_entries(directory):
            if not self.budget.take():
                return
            self.record(entry, prefix + entry.name, archive_level)

    def record(self, entry: os.DirEntry[str], path: str, archive_level: bool) -> None:
        if entry.is_symlink():
            self.skipped_symlinks += 1
        elif entry.is_dir(follow_symlinks=False):
            self.record_directory(entry, path, archive_level)
        elif (size := entry_size(entry)) is not None:
            self.files.append(MediaFile(path=path, size=size))

    def record_directory(
        self, entry: os.DirEntry[str], path: str, archive_level: bool
    ) -> None:
        if not archive_level and entry.name == PGN_ARCHIVE_DIR:
            self.scan_level(entry.path, prefix=path + "/", archive_level=True)
        else:
            size = sum(walk_files(entry.path, self.budget))
            self.files.append(MediaFile(path=path + "/", size=size, is_dir=True))


def scan_media(root: Path, budget: ScanBudget | None = None) -> MediaScan:
    return MediaScanner(root, budget or ScanBudget()).scan()
