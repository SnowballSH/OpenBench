import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.db import close_old_connections
from django.utils import timezone

from OpenBench import upstream
from OpenBench.models import DefaultBranchCommit, EngineConfig, Test
from OpenBench.releases import store
from OpenBench.releases.domain import (
    MAX_STANDING_CHECKS,
    PASS_DEADLINE_SECONDS,
    RATE_LIMIT_PAUSE,
    RECHECK_INTERVAL,
    REFRESH_INTERVAL,
    STANDINGS_PER_PASS,
    PassEnded,
    ProviderError,
    RateLimited,
    ReleaseAnchor,
    ReleaseProvider,
    Repository,
    failure_backoff,
    same_repository,
)
from OpenBench.releases.github import GitHubReleases, api_url

LOGGER = logging.getLogger(__name__)

PASS_INTERVAL_SECONDS = 600.0
MEASURING_MODE = 'GAMES'

type StopCheck = Callable[[], bool]


@dataclass(frozen=True, slots=True)
class Outcome:
    engine: str
    attempted: bool
    error: str
    standings: int
    retry_at: datetime | None = None


@dataclass(slots=True)
class PassClock:
    next_pass: float = 0.0

    def due(self) -> bool:
        if time.monotonic() < self.next_pass:
            return False
        self.next_pass = time.monotonic() + PASS_INTERVAL_SECONDS
        return True

    def postpone(self, wait: timedelta) -> None:
        self.next_pass = max(self.next_pass, time.monotonic() + wait.total_seconds())


@dataclass(frozen=True, slots=True)
class PassGuard:
    should_stop: StopCheck
    deadline: float

    def __call__(self) -> None:
        if self.should_stop() or time.monotonic() >= self.deadline:
            raise PassEnded('The pass ended before this lookup')


WATCHER_CLOCK = PassClock()


def never_stop() -> bool:
    return False


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
    known = store.load_anchor(config.name)
    store.record_attempt(config.name, now)
    try:
        repository = repository_of(config)
        release = provider.latest_release(repository)
        branch = provider.default_branch(repository)
    except PassEnded:
        store.record_attempt(config.name, known.attempted_at if known else None)
        raise
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
        test_mode=MEASURING_MODE,
        dev_engine=anchor.engine,
        base_engine=anchor.engine,
        base__sha=anchor.sha,
    ).exclude(dev__sha=anchor.sha)
    # A test created from the branch's own name built its head, which needs no lookup
    rows = tests.order_by('-id').values_list('dev__sha', 'dev__name', 'dev_repo')
    named = {sha for sha, name, repo in rows if name == anchor.default_branch and same_repository(repo, source)}
    return list(dict.fromkeys(sha for sha, _, _ in rows if sha not in named))


def recheck_due(row: DefaultBranchCommit, now: datetime) -> bool:
    if row.failures:
        return now - row.checked_at >= failure_backoff(row.failures)
    return row.checks < MAX_STANDING_CHECKS and now - row.checked_at >= RECHECK_INTERVAL


def unresolved(anchor: ReleaseAnchor, source: str, now: datetime, force: bool) -> list[str]:
    devs = anchor_devs(anchor, source)
    checked = {row.sha: row for row in DefaultBranchCommit.objects.filter(engine=anchor.engine, sha__in=devs)}

    def pending(sha: str) -> bool:
        row = checked.get(sha)
        if row is None:
            return True
        return not row.on_default_branch and (force or recheck_due(row, now))

    return [sha for sha in devs if pending(sha)]


def resolve_standings(
    config: EngineConfig, anchor: ReleaseAnchor, provider: ReleaseProvider, now: datetime, force: bool = False
) -> tuple[int, str]:
    if not anchor.known or not anchor.default_branch:
        return 0, ''
    pending = unresolved(anchor, config.source, now, force)[:STANDINGS_PER_PASS]
    if not pending:
        return 0, ''
    repository = repository_of(config)
    resolved, failure = 0, ''
    for sha in pending:
        try:
            standing = provider.standing(repository, anchor.default_branch, sha)
        except PassEnded:
            raise
        except ProviderError as error:
            store.record_standing_failure(config.name, sha, now)
            if isinstance(error, RateLimited):
                raise
            failure = str(error)
            continue
        store.record_standing(config.name, standing, now)
        resolved += 1
    return resolved, failure


def refresh_engine(config: EngineConfig, provider: ReleaseProvider, now: datetime, force: bool = False) -> Outcome:
    anchor = store.load_anchor(config.name)
    attempted = (force and not (anchor and anchor.pinned)) or refresh_due(anchor, now)
    error = refresh_release(config, provider, now) if attempted else ''
    anchor = store.load_anchor(config.name)
    if error or anchor is None:
        return Outcome(config.name, attempted, error, 0)
    standings, failure = resolve_standings(config, anchor, provider, now, force)
    return Outcome(config.name, attempted, failure, standings)


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
        except PassEnded:
            break
        except RateLimited as error:
            outcomes.append(Outcome(config.name, False, str(error), 0, error.reset_at or now + RATE_LIMIT_PAUSE))
            break
        except Exception as error:
            # One engine's failure, whatever it is, must not cost the others their turn
            if not isinstance(error, ProviderError):
                LOGGER.exception('Release refresh for %s failed', config.name)
            outcomes.append(Outcome(config.name, False, str(error) or type(error).__name__, 0))
    return outcomes


def pause_after(outcomes: list[Outcome], now: datetime) -> timedelta | None:
    waits = [outcome.retry_at - now for outcome in outcomes if outcome.retry_at is not None]
    return min(max(waits), 2 * RATE_LIMIT_PAUSE) if waits else None


def refresh_when_due(should_stop: StopCheck = never_stop, clock: PassClock = WATCHER_CLOCK) -> None:
    """Called by the watcher thread on every loop: never raises, and reaches the network only when due."""

    if not clock.due():
        return
    try:
        now = timezone.now()
        guard = PassGuard(should_stop, time.monotonic() + PASS_DEADLINE_SECONDS)
        outcomes = refresh_all(GitHubReleases(before_call=guard), now)
        for outcome in outcomes:
            if outcome.error:
                LOGGER.warning('Release refresh for %s failed: %s', outcome.engine, outcome.error)
        if (wait := pause_after(outcomes, now)) is not None:
            clock.postpone(wait)
    except Exception:
        LOGGER.exception('Release refresh failed')
        close_old_connections()
