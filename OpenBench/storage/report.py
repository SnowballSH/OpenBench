import heapq
from collections import defaultdict
from collections.abc import Callable, Iterable
from collections.abc import Set as AbstractSet
from pathlib import Path
from urllib.parse import quote

from django.conf import settings
from django.core.cache import cache
from django.db import connection
from django.utils import timezone

from OpenBench.models import EngineConfig, LogEvent, Network, Test
from OpenBench.storage.classify import Classified, classify
from OpenBench.storage.domain import (
    Category,
    CategoryUsage,
    EngineNetworks,
    StorageItem,
    StorageReport,
)
from OpenBench.storage.scan import (
    Walker,
    database_usage,
    directory_usage,
    disk_usage,
    network_file_sizes,
    scan_media,
)

CACHE_KEY = 'openbench:storage-report'
CACHE_SECONDS = 60
LARGEST_ITEMS = 10

type NetworkRow = tuple[str, str, str]
type ItemBuilder = Callable[[list[Classified]], list[StorageItem]]


def networks_url(engine: str, configured: AbstractSet[str]) -> str | None:
    return f'/networks/{quote(engine, safe="")}/' if engine in configured else None


def workload_links(test_ids: Iterable[int]) -> dict[int, str]:
    tests = Test.objects.filter(id__in=set(test_ids)).only('id', 'test_mode')
    return {test.id: f'/{test.workload_type_str()}/{test.id}/' for test in tests}


def owner_url(owners: list[tuple[str, str]], configured: AbstractSet[str]) -> str | None:
    return next(
        (url for engine, _ in owners if (url := networks_url(engine, configured))),
        None,
    )


def network_items(rows: list[NetworkRow], configured: AbstractSet[str]) -> ItemBuilder:
    owners: defaultdict[str, list[tuple[str, str]]] = defaultdict(list)
    for engine, name, sha256 in rows:
        owners[sha256].append((engine, name))

    def build(files: list[Classified]) -> list[StorageItem]:
        return [
            StorageItem(
                name=item.file.path,
                size=item.file.size,
                detail=', '.join(f'{engine} / {name}' for engine, name in owners[item.file.path]),
                url=owner_url(owners[item.file.path], configured),
            )
            for item in files
        ]

    return build


def workload_items(pending: bool) -> ItemBuilder:
    def build(files: list[Classified]) -> list[StorageItem]:
        links = workload_links(item.test_id for item in files if item.test_id is not None)
        return [
            StorageItem(
                name=item.file.path,
                size=item.file.size,
                detail=workload_detail(item.test_id, item.test_id in links, pending),
                url=links.get(item.test_id) if item.test_id is not None else None,
            )
            for item in files
        ]

    return build


def workload_detail(test_id: int | None, exists: bool, pending: bool) -> str:
    suffix = ', awaiting archive' if pending else ''
    return f'Workload {test_id}{suffix}{"" if exists else " (workload deleted)"}'


def event_log_items(files: list[Classified]) -> list[StorageItem]:
    events = {
        log_file: (pk, summary)
        for log_file, pk, summary in LogEvent.objects.filter(
            log_file__in=[item.file.path for item in files]
        ).values_list('log_file', 'id', 'summary')
    }
    return [
        StorageItem(
            name=item.file.path,
            size=item.file.size,
            detail=events[item.file.path][1] if item.file.path in events else 'No event references this log',
            url=f'/event/{events[item.file.path][0]}/' if item.file.path in events else None,
        )
        for item in files
    ]


def other_detail(item: Classified) -> str:
    if item.file.is_dir:
        return 'Directory'
    if item.unreferenced_network:
        return 'Network file that no Network references'
    return ''


def other_items(files: list[Classified]) -> list[StorageItem]:
    return [
        StorageItem(
            name=item.file.path,
            size=item.file.size,
            detail=other_detail(item),
            url=None,
        )
        for item in files
    ]


def largest(files: list[Classified], count: int) -> list[Classified]:
    return heapq.nlargest(count, files, key=lambda item: (item.file.size, item.file.path))


def category_usage(category: Category, files: list[Classified], count: int, build: ItemBuilder) -> CategoryUsage:
    return CategoryUsage(
        category=category,
        files=len(files),
        size=sum(item.file.size for item in files),
        largest=tuple(build(largest(files, count))),
    )


def engine_networks(
    rows: list[NetworkRow], sizes: dict[str, int], configured: AbstractSet[str]
) -> list[EngineNetworks]:
    by_engine: defaultdict[str, list[str]] = defaultdict(list)
    for engine, _, sha256 in rows:
        by_engine[engine].append(sha256)

    usages = [
        EngineNetworks(
            engine=engine,
            networks=len(shas),
            files=len({sha for sha in shas if sha in sizes}),
            missing=sum(sha not in sizes for sha in shas),
            size=sum(sizes[sha] for sha in set(shas) if sha in sizes),
            url=networks_url(engine, configured),
        )
        for engine, shas in by_engine.items()
    ]
    return sorted(usages, key=lambda usage: (-usage.size, usage.engine))


def build_report(
    media_root: Path,
    data_dir: Path,
    database_path: Path,
    spool_dir: Path | None = None,
    count: int = LARGEST_ITEMS,
    walker: Walker | None = None,
) -> StorageReport:
    scan = scan_media(media_root, walker)
    rows: list[NetworkRow] = list(Network.objects.values_list('engine', 'name', 'sha256'))
    shas = frozenset(sha256 for _, _, sha256 in rows)
    configured = frozenset(EngineConfig.objects.values_list('name', flat=True))

    grouped: defaultdict[Category, list[Classified]] = defaultdict(list)
    for file in scan.files:
        item = classify(file, shas)
        grouped[item.category].append(item)

    builders: dict[Category, ItemBuilder] = {
        Category.NETWORKS: network_items(rows, configured),
        Category.PGN_ARCHIVES: workload_items(pending=False),
        Category.PGN_PENDING: workload_items(pending=True),
        Category.EVENT_LOGS: event_log_items,
        Category.OTHER: other_items,
    }

    return StorageReport(
        generated_at=timezone.now(),
        disk=disk_usage(media_root, data_dir),
        database=database_usage(database_path),
        media_files=len(scan.files),
        media_size=sum(file.size for file in scan.files),
        skipped_symlinks=scan.skipped_symlinks,
        unreadable_dirs=scan.unreadable_dirs,
        truncated=scan.truncated,
        categories=tuple(
            category_usage(category, grouped[category], count, builders[category]) for category in Category
        ),
        networks_by_engine=tuple(engine_networks(rows, network_file_sizes(media_root, shas), configured)),
        upload_spool=directory_usage(spool_dir) if spool_dir else None,
    )


def configured_report() -> StorageReport:
    spool = settings.FILE_UPLOAD_TEMP_DIR
    return build_report(
        media_root=Path(settings.MEDIA_ROOT),
        data_dir=Path(settings.DATA_DIR),
        database_path=Path(connection.settings_dict['NAME']),
        spool_dir=Path(spool) if spool else None,
    )


def current_report() -> StorageReport:
    return cache.get_or_set(CACHE_KEY, configured_report, CACHE_SECONDS)
