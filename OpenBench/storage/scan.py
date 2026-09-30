import os
import shutil
import stat
from collections.abc import Iterable, Iterator
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
SPOOL_ENTRIES = 10_000


class Walker:
    def __init__(self, entries: int = MAX_ENTRIES) -> None:
        self.remaining = entries
        self.exhausted = False
        self.skipped_symlinks = 0
        self.unreadable_dirs = 0

    def take(self) -> bool:
        if self.remaining <= 0:
            self.exhausted = True
            return False
        self.remaining -= 1
        return True

    def entries(self, directory: Path | str) -> list[os.DirEntry[str]]:
        try:
            with os.scandir(directory) as entries:
                return list(entries)
        except (FileNotFoundError, NotADirectoryError):
            return []
        except OSError:
            self.unreadable_dirs += 1
            return []

    def file_sizes(self, directory: Path | str) -> Iterator[int]:
        pending = [directory]
        while pending:
            for entry in self.entries(pending.pop()):
                if not self.take():
                    return
                if entry.is_symlink():
                    self.skipped_symlinks += 1
                elif entry.is_dir(follow_symlinks=False):
                    pending.append(entry.path)
                elif (size := entry_size(entry)) is not None:
                    yield size


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


def regular_file_size(path: Path) -> int | None:
    try:
        status = os.lstat(path)
    except OSError:
        return None
    return status.st_size if stat.S_ISREG(status.st_mode) else None


def is_plain_name(name: str) -> bool:
    return name not in ("", ".", "..") and os.path.basename(name) == name


def network_file_sizes(media_root: Path, shas: Iterable[str]) -> dict[str, int]:
    sizes = {
        sha: regular_file_size(media_root / sha)
        for sha in set(shas)
        if is_plain_name(sha)
    }
    return {sha: size for sha, size in sizes.items() if size is not None}


def disk_usage(media_root: Path, data_dir: Path) -> DiskUsage | None:
    target = media_root if media_root.is_dir() else data_dir
    try:
        usage = shutil.disk_usage(target)
    except OSError:
        return None
    return DiskUsage(total=usage.total, used=usage.used, free=usage.free)


def database_usage(path: Path) -> DatabaseUsage:
    return DatabaseUsage(
        main=file_size(path),
        wal=file_size(path.with_name(path.name + "-wal")),
        shm=file_size(path.with_name(path.name + "-shm")),
    )


def directory_usage(directory: Path, entries: int = SPOOL_ENTRIES) -> DirectoryUsage:
    walker = Walker(entries)
    sizes = list(walker.file_sizes(directory))
    return DirectoryUsage(files=len(sizes), size=sum(sizes), truncated=walker.exhausted)


class MediaScanner:
    def __init__(self, root: Path, walker: Walker) -> None:
        self.root = root
        self.walker = walker
        self.files: list[MediaFile] = []

    def scan(self) -> MediaScan:
        self.scan_level(self.root, prefix="", archive_level=False)
        return MediaScan(
            files=tuple(self.files),
            skipped_symlinks=self.walker.skipped_symlinks,
            unreadable_dirs=self.walker.unreadable_dirs,
            truncated=self.walker.exhausted,
        )

    def scan_level(
        self, directory: Path | str, prefix: str, archive_level: bool
    ) -> None:
        for entry in self.walker.entries(directory):
            if not self.walker.take():
                return
            self.record(entry, prefix + entry.name, archive_level)

    def record(self, entry: os.DirEntry[str], path: str, archive_level: bool) -> None:
        if entry.is_symlink():
            self.walker.skipped_symlinks += 1
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
            size = sum(self.walker.file_sizes(entry.path))
            self.files.append(MediaFile(path=path + "/", size=size, is_dir=True))


def scan_media(root: Path, walker: Walker | None = None) -> MediaScan:
    return MediaScanner(root, walker or Walker()).scan()
