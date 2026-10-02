import io
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest import mock

import requests
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import SimpleTestCase, TestCase, TransactionTestCase

from OpenBench.models import DefaultBranchCommit, Engine, EngineConfig, EngineRelease, Network
from OpenBench.releases import service, store
from OpenBench.releases.domain import (
    CONNECT_TIMEOUT_SECONDS,
    FAILURE_BACKOFF,
    MAX_STANDING_CHECKS,
    RATE_LIMIT_PAUSE,
    RECHECK_INTERVAL,
    REFRESH_INTERVAL,
    REQUEST_TIMEOUT_SECONDS,
    STANDINGS_PER_PASS,
    BranchStanding,
    PassEnded,
    ProviderError,
    RateLimited,
    Release,
    Repository,
    Unreachable,
)
from OpenBench.releases.github import GitHubReleases, Reply, api_url, http_get
from OpenBench.tests.fixtures import create_engine_config, create_test, create_user, ensure_book, present

NOW = datetime(2026, 10, 2, 12, tzinfo=UTC)
API = 'https://api.github.com/repos/SnowballSH/Avalanche'
REPOSITORY = Repository(API)
RELEASE_SHA = '8b6fa5102a98847b7e03d82a0cb266d3cc888a86'
MERGE_SHA = '26d4d777659555775ec0507450e8b117023d1d2f'
BRANCH_SHA = 'dc55a76000000000000000000000000000000000'
LIMITS = {'x-ratelimit-limit': '60', 'x-ratelimit-remaining': '59', 'x-ratelimit-resource': 'core'}

# Trimmed from the answers api.github.com gave on 2026-10-02; keys kept verbatim
LATEST_RELEASE = {
    'url': f'{API}/releases/367279960',
    'html_url': 'https://github.com/SnowballSH/Avalanche/releases/tag/v4.0.0',
    'id': 367279960,
    'tag_name': 'v4.0.0',
    'target_commitish': 'master',
    'name': 'v4.0.0',
    'draft': False,
    'immutable': False,
    'prerelease': False,
    'created_at': '2026-08-08T18:28:59Z',
    'updated_at': '2026-08-08T19:08:13Z',
    'published_at': '2026-08-08T19:08:13Z',
    'assets': [],
}
LIGHTWEIGHT_TAG = {
    'ref': 'refs/tags/v4.0.0',
    'node_id': 'REF_kwDOGr6TqrByZWZzL3RhZ3MvdjQuMC4w',
    'url': f'{API}/git/refs/tags/v4.0.0',
    'object': {'sha': RELEASE_SHA, 'type': 'commit', 'url': f'{API}/git/commits/{RELEASE_SHA}'},
}
TAG_OBJECT_SHA = '8be58deda2ccce7d036072ec34439f9fad88204f'
ANNOTATED_REF = {
    'ref': 'refs/tags/v4.0.0',
    'node_id': 'MDM6UmVmMzY1MDI6cmVmcy90YWdzL3YyLjQ1LjA=',
    'url': f'{API}/git/refs/tags/v4.0.0',
    'object': {'sha': TAG_OBJECT_SHA, 'type': 'tag', 'url': f'{API}/git/tags/{TAG_OBJECT_SHA}'},
}
ANNOTATED_TAG = {
    'node_id': 'TA_kwDNjpbaACg4YmU1OGRlZGEyY2NjZTdkMDM2MDcyZWMzNDQzOWY5ZmFkODgyMDRm',
    'sha': TAG_OBJECT_SHA,
    'url': f'{API}/git/tags/{TAG_OBJECT_SHA}',
    'tagger': {'name': 'SnowballSH', 'email': 'someone@example.invalid', 'date': '2026-08-08T18:30:00Z'},
    'object': {'sha': RELEASE_SHA, 'type': 'commit', 'url': f'{API}/git/commits/{RELEASE_SHA}'},
    'tag': 'v4.0.0',
    'message': 'Avalanche 4.0.0\n',
}
REPOSITORY_ANSWER = {
    'id': 448697258,
    'name': 'Avalanche',
    'full_name': 'SnowballSH/Avalanche',
    'default_branch': 'master',
}
NO_RELEASE = {
    'message': 'Not Found',
    'documentation_url': 'https://docs.github.com/rest/releases/releases#get-the-latest-release',
    'status': '404',
}
RATE_LIMIT = {
    'message': (
        "API rate limit exceeded for 3.225.113.178. (But here's the good news: Authenticated requests get a "
        'higher rate limit. Check out the documentation for more details.)'
    ),
    'documentation_url': 'https://docs.github.com/rest/overview/resources-in-the-rest-api#rate-limiting',
}
ANCESTOR = {
    'status': 'behind',
    'ahead_by': 0,
    'behind_by': 32,
    'total_commits': 0,
    'base_commit': {'sha': MERGE_SHA, 'commit': {'committer': {'date': '2026-10-01T15:47:13Z'}}},
    'merge_base_commit': {'sha': RELEASE_SHA, 'commit': {'committer': {'date': '2026-08-08T18:28:59Z'}}},
    'commits': [],
    'files': [],
}
DIVERGED = {
    'status': 'diverged',
    'ahead_by': 1,
    'behind_by': 7,
    'total_commits': 1,
    'base_commit': {'sha': MERGE_SHA, 'commit': {'committer': {'date': '2026-10-01T15:47:13Z'}}},
    'merge_base_commit': {'sha': MERGE_SHA, 'commit': {'committer': {'date': '2026-10-01T15:47:13Z'}}},
    'commits': [{'sha': BRANCH_SHA}],
    'files': [{'filename': 'src/search.zig'}],
}
NO_COMMIT = {
    'message': 'Not Found',
    'documentation_url': 'https://docs.github.com/rest/commits/commits#compare-two-commits',
    'status': '404',
}


class FakeGitHub:
    def __init__(self, answers: Mapping[str, tuple[int, object]], limits: Mapping[str, str] = LIMITS) -> None:
        self.answers = answers
        self.limits = limits
        self.calls: list[tuple[str, Mapping[str, str], tuple[float, float]]] = []

    def __call__(self, url: str, headers: Mapping[str, str], timeout: tuple[float, float]) -> Reply:
        self.calls.append((url, headers, timeout))
        status, body = self.answers[url.removeprefix(API)]
        return Reply(status, body, self.limits)

    @property
    def paths(self) -> list[str]:
        return [url.removeprefix(API) for url, _, _ in self.calls]


RELEASED = {
    '/releases/latest': (200, LATEST_RELEASE),
    '/git/ref/tags/v4.0.0': (200, LIGHTWEIGHT_TAG),
    '': (200, REPOSITORY_ANSWER),
}


class GitHubProviderTests(SimpleTestCase):
    def test_the_latest_release_is_resolved_to_its_commit(self):
        github = FakeGitHub(RELEASED)
        release = present(GitHubReleases(github).latest_release(REPOSITORY))
        self.assertEqual(release, Release('v4.0.0', RELEASE_SHA, datetime(2026, 8, 8, 19, 8, 13, tzinfo=UTC)))
        self.assertEqual(github.paths, ['/releases/latest', '/git/ref/tags/v4.0.0'])
        self.assertEqual({timeout for _, _, timeout in github.calls}, {(3.0, 5.0)})
        self.assertEqual((CONNECT_TIMEOUT_SECONDS, REQUEST_TIMEOUT_SECONDS), (3.0, 5.0))

    def test_an_annotated_tag_is_dereferenced_once(self):
        github = FakeGitHub(
            {
                '/releases/latest': (200, LATEST_RELEASE),
                '/git/ref/tags/v4.0.0': (200, ANNOTATED_REF),
                f'/git/tags/{TAG_OBJECT_SHA}': (200, ANNOTATED_TAG),
            }
        )
        self.assertEqual(present(GitHubReleases(github).latest_release(REPOSITORY)).sha, RELEASE_SHA)
        self.assertEqual(github.paths, ['/releases/latest', '/git/ref/tags/v4.0.0', f'/git/tags/{TAG_OBJECT_SHA}'])

    def test_a_tag_of_a_tag_or_of_a_tree_is_an_error(self):
        nested = {**ANNOTATED_TAG, 'object': {'sha': 'f' * 40, 'type': 'tag'}}
        tree = {**LIGHTWEIGHT_TAG, 'object': {'sha': 'f' * 40, 'type': 'tree'}}
        for answers in (
            {'/git/ref/tags/v4.0.0': (200, ANNOTATED_REF), f'/git/tags/{TAG_OBJECT_SHA}': (200, nested)},
            {'/git/ref/tags/v4.0.0': (200, tree)},
            {'/git/ref/tags/v4.0.0': (200, {'ref': 'refs/tags/v4.0.0'})},
        ):
            github = FakeGitHub({'/releases/latest': (200, LATEST_RELEASE), **answers})
            with self.assertRaises(ProviderError):
                GitHubReleases(github).latest_release(REPOSITORY)

    def test_a_branch_named_like_the_tag_is_never_asked_for(self):
        github = FakeGitHub(RELEASED)
        GitHubReleases(github).latest_release(REPOSITORY)
        self.assertFalse([path for path in github.paths if path.startswith(('/commits/', '/branches/'))])

    def test_a_repository_without_releases_has_none(self):
        github = FakeGitHub({'/releases/latest': (404, NO_RELEASE)})
        self.assertIsNone(GitHubReleases(github).latest_release(REPOSITORY))
        self.assertEqual(github.paths, ['/releases/latest'])

    def test_a_rate_limit_is_its_own_error(self):
        exhausted = {**LIMITS, 'x-ratelimit-remaining': '0'}
        for limits in (exhausted, {}):
            with self.subTest(limits=limits), self.assertRaises(RateLimited) as raised:
                GitHubReleases(FakeGitHub({'/releases/latest': (403, RATE_LIMIT)}, limits)).latest_release(REPOSITORY)
            self.assertIsNone(raised.exception.reset_at)

    def test_a_rate_limit_carries_when_it_resets(self):
        limits = {**LIMITS, 'x-ratelimit-remaining': '0', 'x-ratelimit-reset': '1790975788'}
        with self.assertRaises(RateLimited) as raised:
            GitHubReleases(FakeGitHub({'/releases/latest': (403, RATE_LIMIT)}, limits)).latest_release(REPOSITORY)
        self.assertEqual(raised.exception.reset_at, datetime.fromtimestamp(1790975788, UTC))

    def test_other_failures_are_unreachable(self):
        for status in (403, 500, 502):
            with self.subTest(status=status), self.assertRaises(Unreachable):
                github = FakeGitHub({'/releases/latest': (status, {'message': 'Forbidden'})})
                GitHubReleases(github).latest_release(REPOSITORY)

    def test_a_timeout_is_unreachable_and_bounded(self):
        timeout = requests.Timeout('slow')
        with (
            mock.patch('OpenBench.releases.github.requests.get', side_effect=timeout) as get,
            self.assertRaises(Unreachable) as raised,
        ):
            GitHubReleases().latest_release(REPOSITORY)
        self.assertEqual(get.call_args.kwargs['timeout'], (3.0, 5.0))
        self.assertIs(get.call_args.kwargs['allow_redirects'], False)
        self.assertIn('Timeout', str(raised.exception))

    def test_the_http_layer_parses_json_and_tolerates_other_bodies(self):
        response = mock.Mock(status_code=502, headers={})
        response.json.side_effect = ValueError('not json')
        with mock.patch('OpenBench.releases.github.requests.get', return_value=response):
            self.assertEqual(http_get(API, {}, (3.0, 5.0)), Reply(502, None, {}))

    def test_one_redirect_is_followed_and_only_within_the_api(self):
        moved = mock.Mock(status_code=301, headers={'Location': 'https://api.github.com/repositories/448697258'})
        landed = mock.Mock(status_code=200, headers={})
        landed.json.return_value = REPOSITORY_ANSWER
        with mock.patch('OpenBench.releases.github.requests.get', side_effect=[moved, landed]) as get:
            self.assertEqual(http_get(API, {}, (3.0, 5.0)).body, REPOSITORY_ANSWER)
        self.assertEqual(get.call_args.args[0], 'https://api.github.com/repositories/448697258')

        away = mock.Mock(status_code=302, headers={'Location': 'https://example.invalid/steal'})
        with (
            mock.patch('OpenBench.releases.github.requests.get', side_effect=[away, landed]) as get,
            self.assertRaises(Unreachable),
        ):
            http_get(API, {'Authorization': 'token secret'}, (3.0, 5.0))
        self.assertEqual(get.call_count, 1)

    def test_the_guard_runs_before_every_call_and_can_end_the_pass(self):
        github = FakeGitHub(RELEASED)
        allowed = iter([None, PassEnded('stop')])

        def guard() -> None:
            if (verdict := next(allowed)) is not None:
                raise verdict

        with self.assertRaises(PassEnded):
            GitHubReleases(github, before_call=guard).latest_release(REPOSITORY)
        self.assertEqual(github.paths, ['/releases/latest'])

    def test_a_release_whose_tag_has_no_commit_is_an_error(self):
        github = FakeGitHub({'/releases/latest': (200, LATEST_RELEASE), '/git/ref/tags/v4.0.0': (404, NO_COMMIT)})
        with self.assertRaises(ProviderError):
            GitHubReleases(github).latest_release(REPOSITORY)

    def test_default_branch(self):
        self.assertEqual(GitHubReleases(FakeGitHub(RELEASED)).default_branch(REPOSITORY), 'master')

    def test_an_ancestor_is_on_the_branch_with_its_commit_time(self):
        github = FakeGitHub({f'/compare/master...{RELEASE_SHA}?per_page=1': (200, ANCESTOR)})
        standing = GitHubReleases(github).standing(REPOSITORY, 'master', RELEASE_SHA)
        self.assertEqual(standing, BranchStanding(RELEASE_SHA, True, datetime(2026, 8, 8, 18, 28, 59, tzinfo=UTC)))
        identical = FakeGitHub(
            {f'/compare/master...{MERGE_SHA}?per_page=1': (200, {**ANCESTOR, 'status': 'identical'})}
        )
        self.assertTrue(GitHubReleases(identical).standing(REPOSITORY, 'master', MERGE_SHA).on_default_branch)

    def test_a_diverged_or_unknown_commit_is_not_on_the_branch(self):
        for status, body in ((200, DIVERGED), (200, {**DIVERGED, 'status': 'ahead'}), (404, NO_COMMIT)):
            github = FakeGitHub({f'/compare/master...{BRANCH_SHA}?per_page=1': (status, body)})
            standing = GitHubReleases(github).standing(REPOSITORY, 'master', BRANCH_SHA)
            self.assertEqual(standing, BranchStanding(BRANCH_SHA, False, None))

    def test_branch_names_are_quoted_and_credentials_travel(self):
        github = FakeGitHub({f'/compare/release%2Fnext...{BRANCH_SHA}?per_page=1': (200, DIVERGED)})
        private = Repository(API, {'Authorization': 'token secret'})
        GitHubReleases(github).standing(private, 'release/next', BRANCH_SHA)
        self.assertEqual(github.calls[0][1], {'Authorization': 'token secret'})
        self.assertNotIn('secret', repr(private))

    def test_only_the_github_api_is_reached(self):
        github = FakeGitHub({})
        with self.assertRaises(ProviderError):
            GitHubReleases(github).latest_release(Repository('https://example.invalid/repos/a/b'))
        self.assertEqual(github.calls, [])

    def test_api_url(self):
        self.assertEqual(api_url('https://github.com/SnowballSH/Avalanche'), API)
        self.assertEqual(api_url('https://github.com/SnowballSH/Avalanche/'), API)
        self.assertEqual(api_url('https://github.com/SnowballSH/Avalanche.git'), API)
        for source in (
            'https://gitlab.com/a/b',
            'https://github.com/a',
            'https://github.com/a/b/c',
            'https://github.com/../user',
            'https://github.com/a/..',
            'https://github.com/a/.',
            'https://github.com/a.b/c',
            'https://github.com/a/b?x=1',
            'http://github.com/a/b',
            '',
        ):
            self.assertIsNone(api_url(source), source)


class FakeProvider:
    def __init__(
        self,
        release: Release | None | Exception = None,
        branch: str = 'master',
        standings: Mapping[str, bool | Exception] | None = None,
    ) -> None:
        self.release = release
        self.branch = branch
        self.standings = dict(standings or {})
        self.calls: list[str] = []

    def latest_release(self, repository: Repository) -> Release | None:
        self.calls.append('release')
        if isinstance(self.release, Exception):
            raise self.release
        return self.release

    def default_branch(self, repository: Repository) -> str:
        self.calls.append('branch')
        return self.branch

    def standing(self, repository: Repository, branch: str, sha: str) -> BranchStanding:
        self.calls.append(f'standing {sha[:4]}')
        found = self.standings.get(sha, False)
        if isinstance(found, Exception):
            raise found
        return BranchStanding(sha, found, NOW if found else None)


V4 = Release('v4.0.0', RELEASE_SHA, datetime(2026, 8, 8, 19, 8, 13, tzinfo=UTC))


class RefreshTests(TestCase):
    def setUp(self):
        ensure_book()
        self.config = create_engine_config()
        self.author = create_user('author')

    def anchor_test(self, dev_sha: str, dev_name: str = '', **fields: Any):
        test = create_test(self.author, **{'test_mode': 'GAMES', **fields})
        Engine.objects.filter(id=test.dev_id).update(sha=dev_sha, name=dev_name or dev_sha)
        Engine.objects.filter(id=test.base_id).update(sha=RELEASE_SHA, name='v4.0.0')
        return test

    def test_a_release_is_fetched_once_per_interval(self):
        provider = FakeProvider(V4)
        (first,) = service.refresh_all(provider, NOW)
        self.assertEqual((first.attempted, first.error), (True, ''))
        anchor = present(store.load_anchor('Avalanche'))
        self.assertEqual((anchor.tag, anchor.sha, anchor.default_branch), ('v4.0.0', RELEASE_SHA, 'master'))
        self.assertEqual((anchor.fetched_at, anchor.attempted_at, anchor.published_at), (NOW, NOW, V4.published_at))

        (again,) = service.refresh_all(provider, NOW + REFRESH_INTERVAL - timedelta(seconds=1))
        self.assertFalse(again.attempted)
        self.assertEqual(provider.calls, ['release', 'branch'])

        service.refresh_all(provider, NOW + REFRESH_INTERVAL)
        self.assertEqual(provider.calls, ['release', 'branch'] * 2)

    def test_a_failure_is_recorded_and_the_known_release_kept(self):
        service.refresh_all(FakeProvider(V4), NOW)
        later = NOW + REFRESH_INTERVAL
        failing = FakeProvider(Unreachable('GitHub answered 502'))
        (outcome,) = service.refresh_all(failing, later)
        self.assertEqual(outcome.error, 'GitHub answered 502')
        anchor = present(store.load_anchor('Avalanche'))
        self.assertEqual((anchor.tag, anchor.fetched_at, anchor.attempted_at), ('v4.0.0', NOW, later))
        self.assertEqual(anchor.error, 'GitHub answered 502')

        service.refresh_all(failing, later + timedelta(hours=1))
        self.assertEqual(failing.calls, ['release'])

        service.refresh_all(FakeProvider(V4), later + REFRESH_INTERVAL)
        self.assertEqual(present(store.load_anchor('Avalanche')).error, '')

    def test_a_rate_limit_ends_the_pass(self):
        create_engine_config('Other')
        provider = FakeProvider(RateLimited('GitHub rate limit reached'))
        outcomes = service.refresh_all(provider, NOW)
        self.assertEqual([outcome.engine for outcome in outcomes], ['Avalanche'])
        self.assertEqual(provider.calls, ['release'])
        self.assertEqual(present(store.load_anchor('Avalanche')).error, 'GitHub rate limit reached')
        self.assertIsNone(store.load_anchor('Other'))

    def test_a_repository_without_a_release_is_known_as_such(self):
        service.refresh_all(FakeProvider(None), NOW)
        anchor = present(store.load_anchor('Avalanche'))
        self.assertFalse(anchor.known)
        self.assertEqual((anchor.fetched_at, anchor.error), (NOW, ''))

    def test_a_new_release_moves_the_anchor(self):
        service.refresh_all(FakeProvider(V4), NOW)
        newer = Release('v4.1.0', 'c' * 40, NOW)
        service.refresh_all(FakeProvider(newer), NOW + REFRESH_INTERVAL)
        self.assertEqual(present(store.load_anchor('Avalanche')).tag, 'v4.1.0')
        self.assertEqual(EngineRelease.objects.count(), 1)

    def test_a_pinned_release_is_never_fetched(self):
        store.pin_release('Avalanche', V4, 'master', NOW)
        provider = FakeProvider(Release('v9', 'd' * 40, None))
        service.refresh_all(provider, NOW + 10 * REFRESH_INTERVAL)
        service.refresh_all(provider, NOW + 10 * REFRESH_INTERVAL, force=True)
        self.assertEqual(provider.calls, [])
        self.assertEqual(present(store.load_anchor('Avalanche')).tag, 'v4.0.0')

    def test_force_ignores_the_interval(self):
        provider = FakeProvider(V4)
        service.refresh_all(provider, NOW)
        service.refresh_all(provider, NOW + timedelta(minutes=1), force=True)
        self.assertEqual(provider.calls, ['release', 'branch'] * 2)

    def test_disabled_and_unnamed_engines_are_skipped(self):
        other = create_engine_config('Other')
        EngineConfig.objects.filter(id=other.id).update(enabled=False)
        self.assertEqual([found.engine for found in service.refresh_all(FakeProvider(V4), NOW)], ['Avalanche'])
        self.assertEqual(service.refresh_all(FakeProvider(V4), NOW, engines=['Missing']), [])

    def test_a_private_engine_needs_its_token(self):
        EngineConfig.objects.filter(id=self.config.id).update(private=True)
        provider = FakeProvider(V4)
        with mock.patch('OpenBench.upstream.git_credentials', return_value={}):
            (outcome,) = service.refresh_all(provider, NOW)
        self.assertIn('no access token', outcome.error)
        self.assertEqual(provider.calls, [])

        self.config.refresh_from_db()
        with mock.patch('OpenBench.upstream.git_credentials', return_value={'Authorization': 'token t'}):
            self.assertEqual(service.repository_of(self.config).headers, {'Authorization': 'token t'})

    def test_a_source_outside_github_is_refused(self):
        EngineConfig.objects.filter(id=self.config.id).update(source='https://example.invalid/a/b')
        (outcome,) = service.refresh_all(FakeProvider(V4), NOW)
        self.assertIn('not a GitHub repository', outcome.error)

    def test_anchor_devs_are_resolved_a_few_per_pass(self):
        devs = [f'{index:040x}' for index in range(1, STANDINGS_PER_PASS + 3)]
        for sha in devs:
            self.anchor_test(sha)
        create_test(self.author)
        provider = FakeProvider(V4, standings={devs[-1]: True})

        (outcome,) = service.refresh_all(provider, NOW)
        self.assertEqual(outcome.standings, STANDINGS_PER_PASS)
        self.assertEqual(provider.calls[2:], [f'standing {sha[:4]}' for sha in devs[::-1][:STANDINGS_PER_PASS]])
        self.assertEqual(store.load_default_branch_commits('Avalanche'), {devs[-1]: NOW})

        (second,) = service.refresh_all(provider, NOW + timedelta(minutes=10))
        self.assertEqual(second.standings, 2)
        (third,) = service.refresh_all(provider, NOW + timedelta(minutes=20))
        self.assertEqual(third.standings, 0)
        self.assertEqual(DefaultBranchCommit.objects.count(), len(devs))

    def test_a_branch_commit_is_rechecked_after_the_interval_and_a_merged_one_never(self):
        merged, branch = '1' * 40, '2' * 40
        self.anchor_test(merged)
        self.anchor_test(branch)
        provider = FakeProvider(V4, standings={merged: True})
        service.refresh_all(provider, NOW)

        early = NOW + RECHECK_INTERVAL - timedelta(seconds=1)
        self.assertEqual(service.refresh_all(provider, early)[0].standings, 0)

        provider.standings[branch] = True
        self.assertEqual(service.refresh_all(provider, NOW + RECHECK_INTERVAL)[0].standings, 1)
        self.assertEqual(set(store.load_default_branch_commits('Avalanche')), {merged, branch})
        self.assertEqual(service.refresh_all(provider, NOW + 9 * RECHECK_INTERVAL)[0].standings, 0)
        self.assertEqual(provider.calls.count('standing 1111'), 1)

    def test_rechecks_of_a_branch_commit_are_bounded(self):
        sha = '2' * 40
        self.anchor_test(sha)
        provider = FakeProvider(V4)
        for index in range(MAX_STANDING_CHECKS + 5):
            service.refresh_all(provider, NOW + index * RECHECK_INTERVAL)
        self.assertEqual(provider.calls.count('standing 2222'), MAX_STANDING_CHECKS)
        service.refresh_all(provider, NOW + 100 * RECHECK_INTERVAL, force=True)
        self.assertEqual(provider.calls.count('standing 2222'), MAX_STANDING_CHECKS + 1)

    def test_a_test_built_from_the_branch_name_needs_no_lookup(self):
        self.anchor_test('3' * 40, dev_name='master')
        fork = self.anchor_test('4' * 40, dev_name='master', dev_repo='https://github.com/someone/Avalanche')
        provider = FakeProvider(V4)
        service.refresh_all(provider, NOW)
        self.assertEqual(provider.calls[2:], ['standing 4444'])
        self.assertTrue(fork.dev_repo.endswith('someone/Avalanche'))

    def test_deleted_tunes_and_self_tests_are_not_anchor_runs(self):
        self.anchor_test('5' * 40, deleted=True)
        self.anchor_test('6' * 40, test_mode='SPSA')
        self.anchor_test('9' * 40, test_mode='SPRT')
        self.anchor_test(RELEASE_SHA)
        self.anchor_test('7' * 40, base_engine='Other')
        provider = FakeProvider(V4)
        service.refresh_all(provider, NOW)
        self.assertEqual(provider.calls, ['release', 'branch'])

    def test_a_failed_standing_lookup_backs_off_for_a_growing_bounded_interval(self):
        sha, other = '8' * 40, '7' * 40
        self.anchor_test(sha)
        self.anchor_test(other)
        provider = FakeProvider(V4, standings={sha: Unreachable('GitHub answered 502'), other: True})
        (outcome,) = service.refresh_all(provider, NOW)
        self.assertEqual((outcome.error, outcome.standings), ('GitHub answered 502', 1))
        row = DefaultBranchCommit.objects.get(sha=sha)
        self.assertEqual((row.on_default_branch, row.failures, row.checks), (False, 1, 0))
        self.assertEqual(set(store.load_default_branch_commits('Avalanche')), {other})

        service.refresh_all(provider, NOW + FAILURE_BACKOFF - timedelta(seconds=1))
        self.assertEqual(provider.calls.count('standing 8888'), 1)
        service.refresh_all(provider, NOW + FAILURE_BACKOFF)
        self.assertEqual(provider.calls.count('standing 8888'), 2)
        service.refresh_all(provider, NOW + 2 * FAILURE_BACKOFF + timedelta(minutes=1))
        self.assertEqual(provider.calls.count('standing 8888'), 2)
        service.refresh_all(provider, NOW + 3 * FAILURE_BACKOFF)
        self.assertEqual(provider.calls.count('standing 8888'), 3)

        DefaultBranchCommit.objects.filter(sha=sha).update(failures=40, checked_at=NOW)
        service.refresh_all(provider, NOW + RECHECK_INTERVAL - timedelta(seconds=1))
        self.assertEqual(provider.calls.count('standing 8888'), 3)
        provider.standings[sha] = True
        service.refresh_all(provider, NOW + RECHECK_INTERVAL)
        row.refresh_from_db()
        self.assertEqual((row.on_default_branch, row.failures), (True, 0))

    def test_a_rate_limited_standing_lookup_ends_the_pass_and_names_the_reset(self):
        create_engine_config('Other')
        sha = '8' * 40
        self.anchor_test(sha)
        reset = NOW + timedelta(minutes=37)
        provider = FakeProvider(V4, standings={sha: RateLimited('GitHub rate limit reached', reset)})
        outcomes = service.refresh_all(provider, NOW)
        self.assertEqual([(found.engine, found.retry_at) for found in outcomes], [('Avalanche', reset)])
        self.assertEqual(DefaultBranchCommit.objects.get(sha=sha).failures, 1)
        self.assertEqual(service.pause_after(outcomes, NOW), timedelta(minutes=37))

        provider.standings[sha] = RateLimited('GitHub rate limit reached')
        (unknown,) = service.refresh_all(provider, NOW + RECHECK_INTERVAL)
        self.assertEqual(unknown.retry_at, NOW + RECHECK_INTERVAL + RATE_LIMIT_PAUSE)

    def test_a_rate_limit_postpones_the_next_pass(self):
        clock = service.PassClock()
        limited = [service.Outcome('Avalanche', True, 'GitHub rate limit reached', 0, NOW + timedelta(days=1))]
        with (
            mock.patch('OpenBench.releases.service.refresh_all', return_value=limited) as refresh,
            mock.patch('OpenBench.releases.service.timezone.now', return_value=NOW),
            mock.patch('OpenBench.releases.service.time.monotonic', return_value=1000.0),
            self.assertLogs('OpenBench.releases.service', 'WARNING'),
        ):
            service.refresh_when_due(clock=clock)
            self.assertEqual(clock.next_pass, 1000.0 + 2 * RATE_LIMIT_PAUSE.total_seconds())
            service.refresh_when_due(clock=clock)
        self.assertEqual(refresh.call_count, 1)
        self.assertIsNone(service.pause_after([service.Outcome('Avalanche', True, '', 0)], NOW))

    def test_one_engine_failing_in_any_way_does_not_cost_the_others_their_turn(self):
        create_engine_config('Other')
        calls: list[str] = []

        def refresh(config: EngineConfig, *arguments: Any) -> service.Outcome:
            calls.append(config.name)
            if config.name == 'Avalanche':
                raise RuntimeError('database is locked')
            return service.Outcome(config.name, True, '', 0)

        with (
            mock.patch('OpenBench.releases.service.refresh_engine', side_effect=refresh),
            self.assertLogs('OpenBench.releases.service', 'ERROR'),
        ):
            outcomes = service.refresh_all(FakeProvider(V4), NOW)
        self.assertEqual(calls, ['Avalanche', 'Other'])
        self.assertEqual([found.error for found in outcomes], ['database is locked', ''])

    def test_a_stopped_or_overdue_pass_makes_no_further_call_and_costs_no_attempt(self):
        create_engine_config('Other')
        github = FakeGitHub(RELEASED)
        stopping = iter([False, False, False, True, True, True])
        guard = service.PassGuard(lambda: next(stopping), deadline=float('inf'))
        outcomes = service.refresh_all(GitHubReleases(github, before_call=guard), NOW)
        self.assertEqual([found.engine for found in outcomes], ['Avalanche'])
        self.assertEqual(len(github.calls), 3)
        self.assertIsNone(present(store.load_anchor('Other')).attempted_at)

        overdue = service.PassGuard(service.never_stop, deadline=0.0)
        with self.assertRaises(PassEnded):
            overdue()

    def test_the_watcher_passes_its_stop_flag_to_the_guard(self):
        clock = service.PassClock()
        with mock.patch('OpenBench.releases.service.refresh_all', return_value=[]) as refresh:
            service.refresh_when_due(lambda: True, clock)
        guard = refresh.call_args.args[0].before_call
        with self.assertRaises(PassEnded):
            guard()

    def test_a_new_release_drops_the_recorded_bench_and_network(self):
        service.refresh_all(FakeProvider(V4), NOW)
        self.assertTrue(store.set_metadata('Avalanche', 3_141_592, 'none'))
        service.refresh_all(FakeProvider(V4), NOW + REFRESH_INTERVAL)
        kept = present(store.load_anchor('Avalanche'))
        self.assertEqual((kept.bench, kept.network, kept.pinned), (3_141_592, 'none', False))

        service.refresh_all(FakeProvider(Release('v4.1.0', 'c' * 40, NOW)), NOW + 2 * REFRESH_INTERVAL)
        moved = present(store.load_anchor('Avalanche'))
        self.assertEqual((moved.tag, moved.bench, moved.network), ('v4.1.0', None, ''))

        store.set_metadata('Avalanche', 5, None)
        store.pin_release('Avalanche', Release('v4.1.0', 'c' * 40, NOW), 'master', NOW)
        self.assertEqual(present(store.load_anchor('Avalanche')).bench, 5)
        store.pin_release('Avalanche', V4, 'master', NOW)
        self.assertIsNone(present(store.load_anchor('Avalanche')).bench)

    def test_the_watcher_hook_is_throttled_and_never_raises(self):
        clock = service.PassClock()
        with mock.patch('OpenBench.releases.service.refresh_all', side_effect=RuntimeError('boom')) as refresh:
            with self.assertLogs('OpenBench.releases.service', 'ERROR'):
                service.refresh_when_due(clock=clock)
            service.refresh_when_due(clock=clock)
        self.assertEqual(refresh.call_count, 1)

        clock.next_pass = 0.0
        failed = [service.Outcome('Avalanche', True, 'GitHub rate limit reached', 0)]
        with (
            mock.patch('OpenBench.releases.service.refresh_all', return_value=failed),
            self.assertLogs('OpenBench.releases.service', 'WARNING') as logs,
        ):
            service.refresh_when_due(clock=clock)
        self.assertIn('rate limit', logs.output[0])


class CommandTests(TestCase):
    def setUp(self):
        self.config = create_engine_config()

    def run_command(self, *arguments: str, **options: Any) -> str:
        out = io.StringIO()
        call_command(*arguments, stdout=out, **options)
        return out.getvalue()

    def test_set_release_pins_without_the_network(self):
        with mock.patch('OpenBench.releases.github.requests.get') as get:
            output = self.run_command(
                'set_release', 'Avalanche', 'v4.0.0', RELEASE_SHA.upper(), published='2026-08-08T19:08:13+00:00'
            )
        get.assert_not_called()
        self.assertIn('v4.0.0', output)
        anchor = present(store.load_anchor('Avalanche'))
        self.assertEqual((anchor.tag, anchor.sha, anchor.pinned), ('v4.0.0', RELEASE_SHA, True))
        self.assertEqual((anchor.default_branch, anchor.published_at), ('master', V4.published_at))

    def test_set_release_takes_the_branch_from_the_option_or_the_presets(self):
        self.config.presets = {'test_presets': {'default': {'both_branch': 'main'}}}
        self.config.save()
        self.run_command('set_release', 'Avalanche', 'v1', 'a' * 40)
        self.assertEqual(present(store.load_anchor('Avalanche')).default_branch, 'main')
        self.run_command('set_release', 'Avalanche', 'v1', 'a' * 40, default_branch='trunk')
        self.assertEqual(present(store.load_anchor('Avalanche')).default_branch, 'trunk')
        self.run_command('set_release', 'Avalanche', 'v2', 'b' * 40)
        self.assertEqual(present(store.load_anchor('Avalanche')).default_branch, 'trunk')

    def test_set_release_marks_default_branch_commits(self):
        self.run_command('set_release', 'Avalanche', 'v1', 'a' * 40, on_default_branch=['C' * 40, 'd' * 40])
        self.assertEqual(set(store.load_default_branch_commits('Avalanche')), {'c' * 40, 'd' * 40})
        self.run_command('set_release', 'Avalanche', on_default_branch=['e' * 40])
        self.assertIn('e' * 40, store.load_default_branch_commits('Avalanche'))

    def test_bench_and_network_are_recorded_without_pinning(self):
        Network.objects.create(sha256='ABCDEF01', name='nezha', engine='Avalanche', author='admin')
        service.refresh_all(FakeProvider(V4), NOW)

        output = self.run_command('set_release', 'Avalanche', bench=3141592, network='ABCDEF01')
        self.assertIn('bench and network recorded', output)
        anchor = present(store.load_anchor('Avalanche'))
        self.assertEqual((anchor.bench, anchor.network), (3141592, 'ABCDEF01'))
        self.assertEqual((anchor.pinned, anchor.tag), (False, 'v4.0.0'))

        self.run_command('set_release', 'Avalanche', network='NONE')
        anchor = present(store.load_anchor('Avalanche'))
        self.assertEqual((anchor.bench, anchor.network), (3141592, 'none'))

        self.run_command('set_release', 'Avalanche', 'v4.0.0', RELEASE_SHA, bench=42)
        pinned = present(store.load_anchor('Avalanche'))
        self.assertEqual((pinned.bench, pinned.network, pinned.pinned), (42, 'none', True))

    def test_bench_and_network_need_a_known_release_and_valid_values(self):
        with self.assertRaisesRegex(CommandError, 'No release of Avalanche is known'):
            self.run_command('set_release', 'Avalanche', bench=5)
        service.refresh_all(FakeProvider(V4), NOW)
        for options in ({'bench': 0}, {'bench': -3}, {'network': 'FFFFFFFF'}):
            with self.subTest(options=options), self.assertRaises(CommandError):
                self.run_command('set_release', 'Avalanche', **options)

    def test_set_release_rejects_bad_input(self):
        for arguments, options in (
            (('Missing', 'v1', 'a' * 40), {}),
            (('Avalanche',), {}),
            (('Avalanche', 'v1', 'a' * 40), {'published': 'yesterday'}),
            (('Avalanche', 'v1'), {}),
            (('Avalanche', 'v1', 'a' * 39), {}),
            (('Avalanche', 'v1', 'g' * 40), {}),
            (('Avalanche', 'v1', 'v4.0.0'), {}),
            (('Avalanche', 'v1', 'a' * 40), {'on_default_branch': ['abc1234']}),
        ):
            with self.subTest(arguments=arguments), self.assertRaises(CommandError):
                self.run_command('set_release', *arguments, **options)

    def test_unpin_hands_the_engine_back_to_github(self):
        self.run_command('set_release', 'Avalanche', 'v1', 'a' * 40)
        self.assertIn('follows GitHub again', self.run_command('set_release', 'Avalanche', unpin=True))
        self.assertIn('was not pinned', self.run_command('set_release', 'Avalanche', unpin=True))
        provider = FakeProvider(V4)
        service.refresh_all(provider, NOW)
        self.assertEqual(present(store.load_anchor('Avalanche')).tag, 'v4.0.0')

    def test_refresh_releases_reports_each_engine(self):
        github = FakeGitHub(RELEASED)
        with mock.patch(
            'OpenBench.management.commands.refresh_releases.GitHubReleases', lambda: GitHubReleases(github)
        ):
            first = self.run_command('refresh_releases')
            second = self.run_command('refresh_releases', 'Avalanche')
            forced = self.run_command('refresh_releases', force=True)
        self.assertIn('Avalanche: refreshed; 0 commits checked', first)
        self.assertIn('Avalanche: not due', second)
        self.assertIn('Avalanche: refreshed', forced)
        self.assertEqual(github.paths, ['/releases/latest', '/git/ref/tags/v4.0.0', ''] * 2)


class MigrationTests(TransactionTestCase):
    def test_the_release_tables_come_and_go(self):
        tables = {'OpenBench_enginerelease', 'OpenBench_defaultbranchcommit'}
        executor = MigrationExecutor(connection)
        try:
            executor.migrate([('OpenBench', '0021_game_analysis')])
            self.assertFalse(tables & set(connection.introspection.table_names()))
        finally:
            executor = MigrationExecutor(connection)
            executor.migrate(executor.loader.graph.leaf_nodes())
        self.assertTrue(tables <= set(connection.introspection.table_names()))
