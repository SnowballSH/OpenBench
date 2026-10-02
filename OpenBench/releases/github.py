from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import quote

import requests

from OpenBench.releases.domain import (
    REQUEST_TIMEOUT_SECONDS,
    BranchStanding,
    ProviderError,
    RateLimited,
    Release,
    Repository,
    Unreachable,
)

API_ROOT = 'https://api.github.com/repos/'
SITE_ROOT = 'https://github.com/'
ANCESTOR_STATUSES = frozenset({'behind', 'identical'})
NOT_FOUND = 404
THROTTLED = frozenset({403, 429})
OK = 200


@dataclass(frozen=True, slots=True)
class Reply:
    status: int
    body: object
    headers: Mapping[str, str]


type Fetch = Callable[[str, Mapping[str, str], float], Reply]


def http_get(url: str, headers: Mapping[str, str], timeout: float) -> Reply:
    try:
        response = requests.get(url, headers=dict(headers), timeout=timeout)
    except requests.RequestException as error:
        raise Unreachable(f'GitHub did not answer: {type(error).__name__}') from error
    try:
        body: object = response.json()
    except ValueError:
        body = None
    return Reply(response.status_code, body, response.headers)


def api_url(source: str) -> str | None:
    if not source.startswith(SITE_ROOT):
        return None
    path = source.removeprefix(SITE_ROOT).strip('/').removesuffix('.git')
    return f'{API_ROOT}{path}' if path.count('/') == 1 else None


def parse_time(raw: object) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def rate_limited(reply: Reply) -> bool:
    if reply.status not in THROTTLED:
        return False
    message = reply.body.get('message', '') if isinstance(reply.body, dict) else ''
    return reply.headers.get('x-ratelimit-remaining') == '0' or 'rate limit' in str(message).lower()


def commit_time(commit: object) -> datetime | None:
    if not isinstance(commit, dict):
        return None
    details = commit.get('commit')
    committer = details.get('committer') if isinstance(details, dict) else None
    return parse_time(committer.get('date')) if isinstance(committer, dict) else None


def found(reply: Reply, what: str) -> dict[str, Any]:
    if reply.status == NOT_FOUND or not isinstance(reply.body, dict):
        raise ProviderError(f'GitHub has no {what}')
    return reply.body


class GitHubReleases:
    def __init__(self, fetch: Fetch = http_get, timeout: float = REQUEST_TIMEOUT_SECONDS) -> None:
        self.fetch = fetch
        self.timeout = timeout

    def get(self, repository: Repository, *path: str, query: str = '') -> Reply:
        if not repository.api_url.startswith(API_ROOT):
            raise ProviderError('Only the GitHub API is reached')
        url = '/'.join([repository.api_url, *path]) + query
        reply = self.fetch(url, repository.headers, self.timeout)
        if rate_limited(reply):
            raise RateLimited('GitHub rate limit reached')
        if reply.status not in (OK, NOT_FOUND):
            raise Unreachable(f'GitHub answered {reply.status}')
        return reply

    def latest_release(self, repository: Repository) -> Release | None:
        reply = self.get(repository, 'releases', 'latest')
        if reply.status == NOT_FOUND:
            return None
        release = found(reply, 'release')
        tag = release.get('tag_name')
        if not isinstance(tag, str) or not tag:
            raise ProviderError('GitHub named no tag for the latest release')
        sha = found(self.get(repository, 'commits', quote(tag, safe='')), f'commit for the tag {tag}').get('sha')
        if not isinstance(sha, str) or not sha:
            raise ProviderError(f'GitHub named no commit for the tag {tag}')
        return Release(tag, sha, parse_time(release.get('published_at')))

    def default_branch(self, repository: Repository) -> str:
        branch = found(self.get(repository), 'such repository').get('default_branch')
        if not isinstance(branch, str) or not branch:
            raise ProviderError('GitHub named no default branch')
        return branch

    def standing(self, repository: Repository, branch: str, sha: str) -> BranchStanding:
        # Compared from the branch to the commit: for an ancestor the answer lists no commits or files
        span = f'{quote(branch, safe="")}...{quote(sha, safe="")}'
        reply = self.get(repository, 'compare', span, query='?per_page=1')
        if reply.status == NOT_FOUND or not isinstance(reply.body, dict):
            return BranchStanding(sha, False, None)
        on_branch = reply.body.get('status') in ANCESTOR_STATUSES
        return BranchStanding(sha, on_branch, commit_time(reply.body.get('merge_base_commit')) if on_branch else None)
