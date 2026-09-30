from dataclasses import dataclass
from datetime import datetime

from OpenBench.storage.domain import (
    LOW_FREE_BYTES,
    LOW_FREE_FRACTION,
    Category,
    CategoryUsage,
    EngineNetworks,
    StorageItem,
    StorageReport,
)
from OpenBench.storage.report import CACHE_SECONDS

UNITS = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")


def format_bytes(size: int) -> str:
    value, exponent = float(size), 0
    while abs(value) >= 1024 and exponent < len(UNITS) - 1:
        value, exponent = value / 1024, exponent + 1
    return f"{size} B" if exponent == 0 else f"{value:.1f} {UNITS[exponent]}"


def count(amount: int, noun: str) -> str:
    return f"{amount} {noun}{'' if amount == 1 else 's'}"


def format_fraction(fraction: float) -> str:
    return f"{min(max(fraction, 0.0), 1.0):.4f}"


def format_percent(fraction: float) -> str:
    return f"{100 * fraction:.1f}%"


@dataclass(frozen=True, slots=True)
class Tile:
    label: str
    value: str
    meta: str
    tone: str | None = None
    fraction: str | None = None


@dataclass(frozen=True, slots=True)
class CategoryRow:
    label: str
    files: int
    size: str
    share: str
    percent: str


@dataclass(frozen=True, slots=True)
class ItemRow:
    name: str
    size: str
    detail: str
    url: str | None


@dataclass(frozen=True, slots=True)
class LargestTable:
    label: str
    rows: tuple[ItemRow, ...]


@dataclass(frozen=True, slots=True)
class EngineRow:
    engine: str
    url: str
    networks: int
    files: int
    missing: int
    size: str


@dataclass(frozen=True, slots=True)
class StoragePage:
    generated_at: datetime
    cache_seconds: int
    tiles: tuple[Tile, ...]
    categories: tuple[CategoryRow, ...]
    largest: tuple[LargestTable, ...]
    engines: tuple[EngineRow, ...]
    spool: str | None
    skipped_symlinks: int
    truncated: bool
    low_disk: bool
    low_disk_rule: str


def disk_tile(report: StorageReport) -> Tile:
    if (disk := report.disk) is None:
        return Tile(
            "Disk free",
            "Unknown",
            "The data directory could not be measured",
            tone="warn",
        )
    return Tile(
        label="Disk free",
        value=format_bytes(disk.free),
        meta=f"{format_percent(disk.free_fraction)} free of {format_bytes(disk.total)}",
        tone="warn" if disk.low else "pass",
        fraction=format_fraction(1 - disk.free_fraction),
    )


def database_tile(report: StorageReport) -> Tile:
    database = report.database
    meta = (
        f"WAL {format_bytes(database.wal)}, shared memory {format_bytes(database.shm)}"
    )
    return Tile("Database", format_bytes(database.total), meta, tone="info")


def media_tile(report: StorageReport) -> Tile:
    return Tile(
        "Media",
        format_bytes(report.media_size),
        count(report.media_files, "file"),
        tone="info",
    )


def networks_tile(report: StorageReport) -> Tile:
    usage = report.category(Category.NETWORKS)
    networks = sum(engine.networks for engine in report.networks_by_engine)
    meta = ", ".join(
        (
            count(networks, "network"),
            count(usage.files, "file"),
            count(len(report.networks_by_engine), "engine"),
        )
    )
    return Tile("Networks", format_bytes(usage.size), meta, tone="info")


def category_row(usage: CategoryUsage, media_size: int) -> CategoryRow:
    share = usage.size / media_size if media_size else 0.0
    return CategoryRow(
        label=usage.category.label,
        files=usage.files,
        size=format_bytes(usage.size),
        share=format_fraction(share),
        percent=format_percent(share),
    )


def item_row(item: StorageItem) -> ItemRow:
    return ItemRow(
        name=item.name, size=format_bytes(item.size), detail=item.detail, url=item.url
    )


def largest_table(usage: CategoryUsage) -> LargestTable:
    return LargestTable(
        label=usage.category.label, rows=tuple(item_row(item) for item in usage.largest)
    )


def engine_row(usage: EngineNetworks) -> EngineRow:
    return EngineRow(
        engine=usage.engine,
        url=usage.url,
        networks=usage.networks,
        files=usage.files,
        missing=usage.missing,
        size=format_bytes(usage.size),
    )


def spool_text(report: StorageReport) -> str | None:
    if (spool := report.upload_spool) is None:
        return None
    return f"{format_bytes(spool.size)} in {count(spool.files, 'file')}"


def storage_page(report: StorageReport) -> StoragePage:
    return StoragePage(
        generated_at=report.generated_at,
        cache_seconds=CACHE_SECONDS,
        tiles=(
            disk_tile(report),
            database_tile(report),
            media_tile(report),
            networks_tile(report),
        ),
        categories=tuple(
            category_row(usage, report.media_size) for usage in report.categories
        ),
        largest=tuple(
            largest_table(usage) for usage in report.categories if usage.largest
        ),
        engines=tuple(engine_row(usage) for usage in report.networks_by_engine),
        spool=spool_text(report),
        skipped_symlinks=report.skipped_symlinks,
        truncated=report.truncated,
        low_disk=report.disk is not None and report.disk.low,
        low_disk_rule=f"under {format_bytes(LOW_FREE_BYTES)} or {format_percent(LOW_FREE_FRACTION)} free",
    )
