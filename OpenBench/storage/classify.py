import re
from collections.abc import Set as AbstractSet
from dataclasses import dataclass

from OpenBench.storage.domain import Category, MediaFile

PENDING_PGN = re.compile(r"^(?P<test>\d+)\.\d+\.\d+\.pgn\.bz2$")
PGN_ARCHIVE = re.compile(r"^PGNs/(?P<test>\d+)\.pgn\.tar$")
EVENT_LOG = re.compile(r"^event\d+(?:_[A-Za-z0-9]+)?\.log$")
NETWORK_NAME = re.compile(r"^[0-9A-F]{8}$")


@dataclass(frozen=True, slots=True)
class Classified:
    file: MediaFile
    category: Category
    test_id: int | None = None
    unreferenced_network: bool = False


def classify(file: MediaFile, network_shas: AbstractSet[str]) -> Classified:
    if file.is_dir:
        return Classified(file, Category.OTHER)
    if file.path in network_shas:
        return Classified(file, Category.NETWORKS)
    if match := PGN_ARCHIVE.match(file.path):
        return Classified(file, Category.PGN_ARCHIVES, test_id=int(match["test"]))
    if match := PENDING_PGN.match(file.path):
        return Classified(file, Category.PGN_PENDING, test_id=int(match["test"]))
    if EVENT_LOG.match(file.path):
        return Classified(file, Category.EVENT_LOGS)
    return Classified(
        file, Category.OTHER, unreferenced_network=bool(NETWORK_NAME.match(file.path))
    )
