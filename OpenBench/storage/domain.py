from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

GIB = 1024**3
LOW_FREE_FRACTION = 0.15
LOW_FREE_BYTES = 2 * GIB


class Category(StrEnum):
    NETWORKS = "networks"
    PGN_ARCHIVES = "pgn_archives"
    PGN_PENDING = "pgn_pending"
    EVENT_LOGS = "event_logs"
    OTHER = "other"

    @property
    def label(self) -> str:
        return CATEGORY_LABELS[self]


CATEGORY_LABELS: dict[Category, str] = {
    Category.NETWORKS: "Networks",
    Category.PGN_ARCHIVES: "Archived PGNs",
    Category.PGN_PENDING: "Pending PGN batches",
    Category.EVENT_LOGS: "Event logs",
    Category.OTHER: "Other",
}


@dataclass(frozen=True, slots=True)
class MediaFile:
    path: str
    size: int
    is_dir: bool = False


@dataclass(frozen=True, slots=True)
class MediaScan:
    files: tuple[MediaFile, ...]
    skipped_symlinks: int
    truncated: bool


@dataclass(frozen=True, slots=True)
class DiskUsage:
    total: int
    used: int
    free: int

    @property
    def free_fraction(self) -> float:
        return self.free / self.total if self.total else 0.0

    @property
    def low(self) -> bool:
        return self.free_fraction < LOW_FREE_FRACTION or self.free < LOW_FREE_BYTES


@dataclass(frozen=True, slots=True)
class DatabaseUsage:
    main: int
    wal: int
    shm: int

    @property
    def total(self) -> int:
        return self.main + self.wal + self.shm


@dataclass(frozen=True, slots=True)
class DirectoryUsage:
    files: int
    size: int
    truncated: bool


@dataclass(frozen=True, slots=True)
class StorageItem:
    name: str
    size: int
    detail: str
    url: str | None


@dataclass(frozen=True, slots=True)
class CategoryUsage:
    category: Category
    files: int
    size: int
    largest: tuple[StorageItem, ...]


@dataclass(frozen=True, slots=True)
class EngineNetworks:
    engine: str
    networks: int
    files: int
    missing: int
    size: int
    url: str


@dataclass(frozen=True, slots=True)
class StorageReport:
    generated_at: datetime
    disk: DiskUsage | None
    database: DatabaseUsage
    media_files: int
    media_size: int
    skipped_symlinks: int
    truncated: bool
    categories: tuple[CategoryUsage, ...]
    networks_by_engine: tuple[EngineNetworks, ...]
    upload_spool: DirectoryUsage | None

    def category(self, category: Category) -> CategoryUsage:
        return next(usage for usage in self.categories if usage.category == category)
