import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import requests

from OpenBench.releases.domain import (
    CONNECT_TIMEOUT_SECONDS,
    REQUEST_TIMEOUT_SECONDS,
    BranchStanding,
    ProviderError,
    RateLimited,
    Release,
    Repository,
    Unreachable,
)

API_HOST = 'https://api.github.com/'
API_ROOT = f'{API_HOST}repos/'
SOURCE = re.compile(r'https://github\.com/([A-Za-z0-9-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?')
DOT_SEGMENTS = frozenset({'.', '..'})
ANCESTOR_STATUSES = frozenset({'behind', 'identical'})
REDIRECTS = frozenset({301, 302, 307, 308})
THROTTLED = frozenset({403, 429})
NOT_FOUND = 404
OK = 200
COMMIT = 'commit'
ANNOTATED_TAG = 'tag'

type Timeout = tuple[float, float]
type Fetch = Callable[[str, Mapping[str, str], Timeout], Reply]


@dataclass(frozen=True, slots=True)
class Reply:
    status: int
    body: object
    headers: Mapping[str, str]


def single_get(url: str, headers: Mapping[str, str], timeout: Timeout) -> requests.Response:
    try:
        return requests.get(url, headers=dict(headers), timeout=timeout, allow_redirects=False)
    except requests.RequestException as error:
        raise Unreachable(f'GitHub did not answer: {type(error).__name__}') from error


def http_get(url: str, headers: Mapping[str, str], timeout: Timeout) -> Reply:
    response = single_get(url, headers, timeout)
    if response.status_code in REDIRECTS:
        # A renamed repository answers with one redirect; it is followed once, and only within the API host
        target = response.headers.get('Location', '')
        if not target.startswith(API_HOST):
            raise Unreachable('GitHub redirected outside its API')
        response = single_get(target, headers, timeout)
    try:
        body: object = response.json()
    except ValueError:
        body = None
    return Reply(response.status_code, body, response.headers)


def api_url(source: str) -> str | None:
    found = SOURCE.fullmatch(source)
    if found is None or found.group(2) in DOT_SEGMENTS:
        return None
    return f'{API_ROOT}{found.group(1)}/{found.group(2)}'


def parse_time(raw: object) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def reset_time(headers: Mapping[str, str]) -> datetime | None:
    raw = headers.get('x-ratelimit-reset', '')
    return datetime.fromtimestamp(int(raw), UTC) if raw.isascii() and raw.isdigit() else None


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


def git_object(body: Mapping[str, Any], what: str) -> tuple[str, str]:
    target = body.get('object')
    kind = target.get('type') if isinstance(target, dict) else None
    sha = target.get('sha') if isinstance(target, dict) else None
    if not isinstance(kind, str) or not isinstance(sha, str) or not sha:
        raise ProviderError(f'GitHub named no object for {what}')
    return kind, sha


def unguarded() -> None:
    return None


class GitHubReleases:
    def __init__(
        self,
        fetch: Fetch = http_get,
        before_call: Callable[[], None] = unguarded,
        timeout: Timeout = (CONNECT_TIMEOUT_SECONDS, REQUEST_TIMEOUT_SECONDS),
    ) -> None:
        self.fetch = fetch
        self.before_call = before_call
        self.timeout = timeout

    def get(self, repository: Repository, *path: str, query: str = '') -> Reply:
        if not repository.api_url.startswith(API_ROOT):
            raise ProviderError('Only the GitHub API is reached')
        self.before_call()
        url = '/'.join([repository.api_url, *path]) + query
        reply = self.fetch(url, repository.headers, self.timeout)
        if rate_limited(reply):
            raise RateLimited('GitHub rate limit reached', reset_time(reply.headers))
        if reply.status not in (OK, NOT_FOUND):
            raise Unreachable(f'GitHub answered {reply.status}')
        return reply

    def tag_commit(self, repository: Repository, tag: str) -> str:
        # The tag namespace is named outright, so a branch of the same name can never be what is resolved
        what = f'the tag {tag}'
        ref = found(self.get(repository, 'git', 'ref', 'tags', quote(tag, safe='/')), what)
        kind, sha = git_object(ref, what)
        if kind == ANNOTATED_TAG:
            kind, sha = git_object(found(self.get(repository, 'git', 'tags', sha), what), what)
        if kind != COMMIT:
            raise ProviderError(f'{what} does not name a commit')
        return sha

    def latest_release(self, repository: Repository) -> Release | None:
        reply = self.get(repository, 'releases', 'latest')
        if reply.status == NOT_FOUND:
            return None
        release = found(reply, 'release')
        tag = release.get('tag_name')
        if not isinstance(tag, str) or not tag:
            raise ProviderError('GitHub named no tag for the latest release')
        return Release(tag, self.tag_commit(repository, tag), parse_time(release.get('published_at')))

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
