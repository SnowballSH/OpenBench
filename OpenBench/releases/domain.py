from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

REFRESH_INTERVAL = timedelta(hours=6)
RECHECK_INTERVAL = timedelta(hours=6)
REQUEST_TIMEOUT_SECONDS = 5.0
STANDINGS_PER_PASS = 3
MAX_STANDING_CHECKS = 28
ERROR_LIMIT = 255


@dataclass(frozen=True, slots=True)
class Release:
    tag: str
    sha: str
    published_at: datetime | None


@dataclass(frozen=True, slots=True)
class Repository:
    api_url: str
    headers: Mapping[str, str] = field(default_factory=dict, repr=False)


@dataclass(frozen=True, slots=True)
class BranchStanding:
    sha: str
    on_default_branch: bool
    committed_at: datetime | None


def same_repository(left: str, right: str) -> bool:
    return left.rstrip('/').lower() == right.rstrip('/').lower()


class ProviderError(Exception):
    pass


class RateLimited(ProviderError):
    pass


class Unreachable(ProviderError):
    pass


class ReleaseProvider(Protocol):
    def latest_release(self, repository: Repository) -> Release | None:
        """The newest published release that is neither a draft nor a prerelease, or None when there is none."""

    def default_branch(self, repository: Repository) -> str:
        """The name of the repository's default branch."""

    def standing(self, repository: Repository, branch: str, sha: str) -> BranchStanding:
        """Whether the commit is an ancestor of the branch's head, or the head itself."""


@dataclass(frozen=True, slots=True)
class ReleaseAnchor:
    engine: str
    tag: str
    sha: str
    published_at: datetime | None
    default_branch: str
    pinned: bool
    fetched_at: datetime | None
    attempted_at: datetime | None
    error: str

    @property
    def known(self) -> bool:
        return bool(self.tag and self.sha)
