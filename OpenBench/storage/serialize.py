from OpenBench.insights.serialize import Json
from OpenBench.storage.domain import (
    CategoryUsage,
    DatabaseUsage,
    DirectoryUsage,
    DiskUsage,
    EngineNetworks,
    StorageItem,
    StorageReport,
)
from OpenBench.storage.report import CACHE_SECONDS


def disk_json(disk: DiskUsage | None) -> Json:
    if disk is None:
        return None
    return {
        "total_bytes": disk.total,
        "used_bytes": disk.used,
        "free_bytes": disk.free,
        "free_fraction": round(disk.free_fraction, 4),
        "low": disk.low,
    }


def database_json(database: DatabaseUsage) -> Json:
    return {
        "bytes": database.total,
        "main_bytes": database.main,
        "wal_bytes": database.wal,
        "shm_bytes": database.shm,
    }


def item_json(item: StorageItem) -> Json:
    return {
        "name": item.name,
        "bytes": item.size,
        "detail": item.detail,
        "url": item.url,
    }


def category_json(usage: CategoryUsage) -> Json:
    return {
        "key": usage.category.value,
        "label": usage.category.label,
        "files": usage.files,
        "bytes": usage.size,
        "largest": [item_json(item) for item in usage.largest],
    }


def engine_json(usage: EngineNetworks) -> Json:
    return {
        "engine": usage.engine,
        "networks": usage.networks,
        "files": usage.files,
        "missing_files": usage.missing,
        "bytes": usage.size,
        "url": usage.url,
    }


def spool_json(spool: DirectoryUsage | None) -> Json:
    if spool is None:
        return None
    return {"files": spool.files, "bytes": spool.size}


def report_json(report: StorageReport) -> Json:
    return {
        "generated_at": report.generated_at.isoformat(),
        "cache_seconds": CACHE_SECONDS,
        "disk": disk_json(report.disk),
        "database": database_json(report.database),
        "media": {
            "files": report.media_files,
            "bytes": report.media_size,
            "skipped_symlinks": report.skipped_symlinks,
            "truncated": report.truncated,
            "categories": [category_json(usage) for usage in report.categories],
        },
        "networks_by_engine": [
            engine_json(usage) for usage in report.networks_by_engine
        ],
        "upload_spool": spool_json(report.upload_spool),
    }
