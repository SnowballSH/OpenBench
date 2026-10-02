import logging
import time
from dataclasses import dataclass
from datetime import datetime

from django.db import close_old_connections
from django.utils import timezone

from OpenBench import upstream
from OpenBench.models import DefaultBranchCommit, EngineConfig, Test
from OpenBench.releases import store
from OpenBench.releases.domain import (
    MAX_STANDING_CHECKS,
    RECHECK_INTERVAL,
    REFRESH_INTERVAL,
    STANDINGS_PER_PASS,
    ProviderError,
    RateLimited,
    ReleaseAnchor,
    ReleaseProvider,
    Repository,
    same_repository,
)
from OpenBench.releases.github import GitHubReleases, api_url

LOGGER = logging.getLogger(__name__)

PASS_INTERVAL_SECONDS = 600.0
TEST_MODES = ('SPRT', 'GAMES')


@dataclass(frozen=True, slots=True)
class Outcome:
    engine: str
    attempted: bool
    error: str
    standings: int


@dataclass(slots=True)
class PassClock:
    next_pass: float = 0.0

    def due(self) -> bool:
        if time.monotonic() < self.next_pass:
            return False
        self.next_pass = time.monotonic() + PASS_INTERVAL_SECONDS
        return True


WATCHER_CLOCK = PassClock()


def repository_of(config: EngineConfig) -> Repository:
    url = api_url(config.source)
    if url is None:
        raise ProviderError('The engine source is not a GitHub repository')
    headers = upstream.git_credentials(config.name) if config.private else {}
    if config.private and not headers:
        raise ProviderError('The server has no access token for this private engine')
    return Repository(url, headers)


def refresh_due(anchor: ReleaseAnchor | None, now: datetime) -> bool:
    if anchor is None:
        return True
    if anchor.pinned:
        return False
    return anchor.attempted_at is None or now - anchor.attempted_at >= REFRESH_INTERVAL


def refresh_release(config: EngineConfig, provider: ReleaseProvider, now: datetime) -> str:
    # The attempt is recorded before the network is touched, so a crash cannot repeat it early
    store.record_attempt(config.name, now)
    try:
        repository = repository_of(config)
        release = provider.latest_release(repository)
        branch = provider.default_branch(repository)
    except ProviderError as error:
        store.record_failure(config.name, str(error), now)
        if isinstance(error, RateLimited):
            raise
        return str(error)
    store.record_release(config.name, release, branch, now)
    return ''


def anchor_devs(anchor: ReleaseAnchor, source: str) -> list[str]:
    tests = Test.objects.filter(
        deleted=False,
        test_mode__in=TEST_MODES,
        dev_engine=anchor.engine,
        base_engine=anchor.engine,
        base__sha=anchor.sha,
    ).exclude(dev__sha=anchor.sha)
    # A test created from the branch's own name built its head, which needs no lookup
    rows = tests.order_by('-id').values_list('dev__sha', 'dev__name', 'dev_repo')
    named = {sha for sha, name, repo in rows if name == anchor.default_branch and same_repository(repo, source)}
    return list(dict.fromkeys(sha for sha, _, _ in rows if sha not in named))


def unresolved(anchor: ReleaseAnchor, source: str, now: datetime, force: bool) -> list[str]:
    devs = anchor_devs(anchor, source)
    checked = {row.sha: row for row in DefaultBranchCommit.objects.filter(engine=anchor.engine, sha__in=devs)}

    def pending(sha: str) -> bool:
        row = checked.get(sha)
        if row is None:
            return True
        if row.on_default_branch:
            return False
        return force or (row.checks < MAX_STANDING_CHECKS and now - row.checked_at >= RECHECK_INTERVAL)

    return [sha for sha in devs if pending(sha)]


def resolve_standings(
    config: EngineConfig, anchor: ReleaseAnchor, provider: ReleaseProvider, now: datetime, force: bool = False
) -> int:
    if not anchor.known or not anchor.default_branch:
        return 0
    pending = unresolved(anchor, config.source, now, force)[:STANDINGS_PER_PASS]
    if not pending:
        return 0
    repository = repository_of(config)
    for sha in pending:
        store.record_standing(config.name, provider.standing(repository, anchor.default_branch, sha), now)
    return len(pending)


def refresh_engine(config: EngineConfig, provider: ReleaseProvider, now: datetime, force: bool = False) -> Outcome:
    anchor = store.load_anchor(config.name)
    attempted = (force and not (anchor and anchor.pinned)) or refresh_due(anchor, now)
    error = refresh_release(config, provider, now) if attempted else ''
    anchor = store.load_anchor(config.name)
    if error or anchor is None:
        return Outcome(config.name, attempted, error, 0)
    return Outcome(config.name, attempted, '', resolve_standings(config, anchor, provider, now, force))


def refresh_all(
    provider: ReleaseProvider, now: datetime, force: bool = False, engines: list[str] | None = None
) -> list[Outcome]:
    configs = EngineConfig.objects.filter(enabled=True).order_by('name')
    if engines is not None:
        configs = configs.filter(name__in=engines)
    outcomes: list[Outcome] = []
    for config in configs:
        try:
            outcomes.append(refresh_engine(config, provider, now, force))
        except RateLimited as error:
            outcomes.append(Outcome(config.name, False, str(error), 0))
            break
        except ProviderError as error:
            outcomes.append(Outcome(config.name, False, str(error), 0))
    return outcomes


def refresh_when_due(clock: PassClock = WATCHER_CLOCK) -> None:
    """Called by the watcher thread on every loop: never raises, and reaches the network only when due."""

    if not clock.due():
        return
    try:
        for outcome in refresh_all(GitHubReleases(), timezone.now()):
            if outcome.error:
                LOGGER.warning('Release refresh for %s failed: %s', outcome.engine, outcome.error)
    except Exception:
        LOGGER.exception('Release refresh failed')
        close_old_connections()
