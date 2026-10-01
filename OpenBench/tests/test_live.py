import re
from datetime import timedelta
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from OpenBench.config import OPENBENCH_CONFIG
from OpenBench.insights.domain import WorkloadStatus
from OpenBench.listing_rows import result_label
from OpenBench.live.domain import FRESHNESS, LiveListing, LiveWorkload, Unchanged
from OpenBench.live.listing import live_listing
from OpenBench.live.token import MAX_TOKEN_LENGTH, listing_token, workload_token
from OpenBench.live.workload import live_workload
from OpenBench.models import Machine, SPSARun, Test
from OpenBench.page_queries import unfinished_tests
from OpenBench.templatetags.mytags import longStatBlock, shortStatBlock
from OpenBench.tests.fixtures import (
    create_engine_config,
    create_test,
    create_user,
    credentials,
    ensure_book,
    system_info,
)

LISTING_URL = '/api/live/workloads/'
STATBLOCK = re.compile(r'<div class="statblock statblock-[a-z]*">(.*?)</div>', re.DOTALL)
TAG = re.compile(r'<[^>]+>')
PAGE_SIZE = 25
PAGE_HEAD = 2000


def workload_url(test: Test) -> str:
    return f'/api/live/workload/{test.id}/'


def play(test: Test, games: int = 2) -> None:
    test.games += games
    test.wins += games
    test.save()


def page_stat_lines(content: str) -> list[list[str]]:
    blocks = [block.partition('</span>')[2] for block in STATBLOCK.findall(content)]
    return [[TAG.sub('', line).strip() for line in block.split('<br>')] for block in blocks]


class LiveCase(TestCase):
    def setUp(self) -> None:
        create_engine_config()
        ensure_book()
        self.author = create_user('author', approver=True)
        self.other = create_user('other')
        self.client.force_login(self.author)


class TokenTests(LiveCase):
    def test_a_listing_token_holds_until_a_workload_changes(self) -> None:
        test = create_test(self.author)
        now = timezone.now()
        token = listing_token(unfinished_tests(), now)
        self.assertEqual(listing_token(unfinished_tests(), now), token)

        play(test)

        self.assertNotEqual(listing_token(unfinished_tests(), now), token)

    def test_a_listing_token_changes_when_a_workload_arrives_is_approved_or_finishes(self) -> None:
        now = timezone.now()
        tokens = [listing_token(unfinished_tests(), now)]
        test = create_test(self.author, approved=False)
        tokens.append(listing_token(unfinished_tests(), now))
        Test.objects.filter(id=test.id).update(approved=True)
        tokens.append(listing_token(unfinished_tests(), now))
        Test.objects.filter(id=test.id).update(finished=True, passed=True)
        tokens.append(listing_token(unfinished_tests(), now))

        self.assertEqual(len(set(tokens[:3])), 3)
        self.assertEqual(tokens[3], tokens[0])

    def test_a_listing_token_is_scoped_to_its_author(self) -> None:
        create_test(self.author)
        theirs = create_test(self.other)
        now = timezone.now()
        token = listing_token(unfinished_tests('author'), now)

        play(theirs)

        self.assertEqual(listing_token(unfinished_tests('author'), now), token)
        self.assertNotEqual(listing_token(unfinished_tests('other'), now), token)

    def test_tokens_expire_with_the_freshness_window(self) -> None:
        test = create_test(self.author)
        now = timezone.now()
        later = now + FRESHNESS

        self.assertNotEqual(listing_token(unfinished_tests(), now), listing_token(unfinished_tests(), later))
        self.assertNotEqual(workload_token(test, now), workload_token(test, later))

    def test_a_workload_token_follows_its_games_and_state(self) -> None:
        test = create_test(self.author)
        now = timezone.now()
        token = workload_token(test, now)
        self.assertEqual(workload_token(Test.objects.get(id=test.id), now), token)

        play(test)
        played = workload_token(test, now)
        test.finished = test.passed = True

        self.assertEqual(len({token, played, workload_token(test, now)}), 3)


class ListingTests(LiveCase):
    def listing(self, author: str | None = None) -> LiveListing:
        listing = live_listing(author, None)
        assert isinstance(listing, LiveListing)
        return listing

    def test_rows_are_the_pending_then_the_active_workloads(self) -> None:
        active = create_test(self.author, games=40, wins=20, losses=10, draws=10, currentllr=1.2)
        pending = create_test(self.author, approved=False)
        create_test(self.author, finished=True, passed=True)
        create_test(self.author, deleted=True)

        rows = self.listing().rows

        self.assertEqual([row.id for row in rows], [pending.id, active.id])
        self.assertEqual([row.result.status for row in rows], [WorkloadStatus.PENDING, WorkloadStatus.ACTIVE])
        self.assertEqual([row.result.outcome for row in rows], ['Pending approval', 'Running'])
        self.assertEqual(rows[1].result.games, 40)
        self.assertEqual(rows[1].result.statblock, shortStatBlock(active).split('\n'))

    def test_only_an_active_row_carries_the_progress_bar(self) -> None:
        create_test(self.author, currentllr=1.47)
        create_test(self.author, approved=False)

        pending, active = self.listing().rows

        self.assertIsNone(pending.progress)
        assert active.progress is not None
        self.assertEqual(active.progress.kind, 'llr')
        self.assertAlmostEqual(active.progress.fraction, 0.75)

    def test_a_row_says_what_it_waits_for_and_when_it_was_created(self) -> None:
        create_test(self.author)

        (row,) = self.listing().rows

        assert row.reason is not None and row.moment is not None
        self.assertTrue(row.reason.brief)
        self.assertIn(row.moment.verb, ('created', 'started'))

    def test_the_author_filter_matches_the_user_page(self) -> None:
        mine = create_test(self.author)
        create_test(self.other)

        self.assertEqual([row.id for row in self.listing('author').rows], [mine.id])
        self.assertEqual(self.listing('nobody').rows, [])

    def test_the_machine_status_is_the_index_heading(self) -> None:
        create_test(self.author)
        Machine.objects.create(user=self.author, info=system_info(concurrency=4), mnps=1.5)

        self.assertEqual(self.listing().machine_status, ': 1 Machines / 4 Threads / 6.0 MNPS ')

    def test_a_known_token_answers_unchanged(self) -> None:
        test = create_test(self.author)
        now = timezone.now()
        token = live_listing(None, None, now).token

        self.assertEqual(live_listing(None, token, now), Unchanged(token))

        play(test)

        self.assertIsInstance(live_listing(None, token, now), LiveListing)

    def test_rows_match_what_the_index_renders(self) -> None:
        create_test(self.author, games=40, wins=20, losses=10, draws=10, currentllr=1.2)
        create_test(self.author, approved=False)
        create_test(self.author, test_mode='GAMES', max_games=1000, games=10, wins=4, losses=3, draws=3)

        rendered = page_stat_lines(self.client.get('/index/').content.decode())

        self.assertEqual(rendered, [row.result.statblock for row in self.listing().rows])


class WorkloadTests(LiveCase):
    def workload(self, test: Test) -> LiveWorkload:
        workload = live_workload(Test.objects.select_related('dev', 'base').get(id=test.id), None)
        assert isinstance(workload, LiveWorkload)
        return workload

    def test_a_running_test_carries_its_long_block_and_diagnosis(self) -> None:
        test = create_test(self.author, games=40, wins=20, losses=10, draws=10)

        workload = self.workload(test)

        self.assertEqual(workload.result.status, WorkloadStatus.ACTIVE)
        self.assertEqual(workload.result.statblock, longStatBlock(test).split('\n'))
        assert workload.diagnosis is not None
        self.assertTrue(workload.diagnosis.headline)

    def test_a_finished_test_carries_its_outcome_and_no_diagnosis(self) -> None:
        test = create_test(self.author, finished=True, passed=True)

        workload = self.workload(test)

        self.assertEqual(workload.result.status, WorkloadStatus.PASSED)
        self.assertEqual((workload.result.colour, workload.result.outcome), ('green', 'Passed'))
        self.assertIsNone(workload.diagnosis)

    def test_a_tune_carries_its_short_block(self) -> None:
        tune = create_test(self.author, test_mode='SPSA', workload_size=8)
        SPSARun.objects.create(
            tune=tune,
            reporting_type='BULK',
            distribution_type='SINGLE',
            alpha=0.602,
            gamma=0.101,
            iterations=100,
            pairs_per=8,
            a_ratio=0.1,
        )

        self.assertIn('0/100 Iterations', self.workload(tune).result.statblock)

    def test_a_known_token_answers_unchanged(self) -> None:
        test = create_test(self.author)
        now = timezone.now()
        token = live_workload(test, None, now).token

        self.assertEqual(live_workload(test, token, now), Unchanged(token))

        play(test)

        self.assertIsInstance(live_workload(test, token, now), LiveWorkload)


class ResultLabelTests(LiveCase):
    def test_labels_follow_the_colour_then_the_state(self) -> None:
        test = create_test(self.author, approved=False)
        self.assertEqual(result_label(test, 'green'), 'Passed')
        self.assertEqual(result_label(test, 'blue'), 'Passed, non-regression')
        self.assertEqual(result_label(test, 'yellow'), 'Failed, wins at least losses')
        self.assertEqual(result_label(test, 'red'), 'Failed')
        self.assertEqual(result_label(test, ''), 'Pending approval')
        test.approved = True
        self.assertEqual(result_label(test, ''), 'Running')
        test.finished = True
        self.assertEqual(result_label(test, ''), 'Finished')


class EndpointTests(LiveCase):
    def test_a_browser_session_polls_the_listing(self) -> None:
        test = create_test(self.author, games=40, wins=20, losses=10, draws=10)

        payload = self.client.get(LISTING_URL).json()

        self.assertTrue(payload['changed'])
        (row,) = payload['rows']
        self.assertEqual(row['id'], test.id)
        self.assertEqual(row['result']['status'], 'active')
        self.assertEqual(row['result']['games'], 40)
        self.assertEqual(
            set(row), {'id', 'result', 'progress', 'timing', 'reason', 'moment'}, 'the documented row fields'
        )

    def test_an_unchanged_listing_is_a_token_and_nothing_else(self) -> None:
        create_test(self.author)
        frozen = timezone.now()
        with mock.patch.object(timezone, 'now', return_value=frozen):
            token = self.client.get(LISTING_URL).json()['token']
            response = self.client.get(LISTING_URL, {'token': token})

        self.assertEqual(response.json(), {'token': token, 'changed': False})
        self.assertLess(len(response.content), 100)

    def test_the_listing_takes_an_author(self) -> None:
        mine = create_test(self.author)
        create_test(self.other)

        rows = self.client.get(LISTING_URL, {'author': 'author'}).json()['rows']

        self.assertEqual([row['id'] for row in rows], [mine.id])

    def test_a_browser_session_polls_a_workload(self) -> None:
        test = create_test(self.author)
        frozen = timezone.now()
        with mock.patch.object(timezone, 'now', return_value=frozen):
            payload = self.client.get(workload_url(test)).json()
            unchanged = self.client.get(workload_url(test), {'token': payload['token']}).json()

        self.assertEqual(payload['workload']['id'], test.id)
        self.assertEqual(payload['workload']['result']['status'], 'active')
        self.assertEqual(set(payload['workload']['diagnosis']), {'state', 'severity', 'headline', 'brief', 'evidence'})
        self.assertEqual(unchanged, {'token': payload['token'], 'changed': False})

    def test_a_finished_workload_reports_its_verdict(self) -> None:
        test = create_test(self.author, finished=True, failed=True, wins=1, losses=5, games=6)

        result = self.client.get(workload_url(test)).json()['workload']['result']

        self.assertEqual((result['status'], result['colour'], result['outcome']), ('failed', 'red', 'Failed'))

    def test_scripts_post_their_credentials(self) -> None:
        test = create_test(self.author)
        self.client.logout()

        listing = self.client.post(LISTING_URL, credentials(self.author))
        workload = self.client.post(workload_url(test), credentials(self.author))

        self.assertEqual((listing.status_code, workload.status_code), (200, 200))

    def test_anonymous_polls_are_refused_while_viewing_needs_a_login(self) -> None:
        test = create_test(self.author)
        self.client.logout()

        for url in (LISTING_URL, workload_url(test)):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 401, url)
            self.assertEqual(response.json(), {'error': 'API requires authentication for this server'})

    def test_a_disabled_account_is_refused(self) -> None:
        self.client.force_login(create_user('disabled', enabled=False))

        self.assertEqual(self.client.get(LISTING_URL).status_code, 401)

    def test_a_public_server_answers_anonymous_polls(self) -> None:
        test = create_test(self.author)
        self.client.logout()

        with mock.patch.dict(OPENBENCH_CONFIG, {'require_login_to_view': False}):
            statuses = [self.client.get(url).status_code for url in (LISTING_URL, workload_url(test))]

        self.assertEqual(statuses, [200, 200])

    def test_an_unknown_workload_is_not_found(self) -> None:
        response = self.client.get('/api/live/workload/999/')

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {'error': 'Requested Workload Id does not exist'})

    def test_oversized_parameters_are_refused(self) -> None:
        test = create_test(self.author)
        long_token = 'a' * (MAX_TOKEN_LENGTH + 1)

        self.assertEqual(self.client.get(LISTING_URL, {'token': long_token}).status_code, 400)
        self.assertEqual(self.client.get(LISTING_URL, {'author': 'a' * 151}).status_code, 400)
        self.assertEqual(self.client.get(workload_url(test), {'token': long_token}).status_code, 400)


class PageHookTests(LiveCase):
    def test_the_index_polls_only_while_something_is_unfinished(self) -> None:
        create_test(self.author, finished=True, passed=True)
        idle = self.client.get('/index/').content.decode()
        test = create_test(self.author, games=12)
        busy = self.client.get('/index/').content.decode()

        self.assertNotIn('live.js', idle)
        self.assertNotIn('data-live-listing', idle)
        self.assertIn('live.js', busy)
        self.assertIn('data-live-listing="/api/live/workloads/"', busy)
        self.assertIn(f'data-live-row="{test.id}" data-live-status="active" data-live-games="12"', busy)
        self.assertEqual(busy.count('data-live-row='), 1)
        self.assertIn('aria-live="polite" data-live-announcer', busy)

    def test_later_pages_and_greens_never_poll(self) -> None:
        create_test(self.author)
        for _ in range(PAGE_SIZE + 1):
            create_test(self.author, finished=True, passed=True)

        for url in ('/index/2/', '/greens/'):
            self.assertNotIn('live.js', self.client.get(url).content.decode()[:PAGE_HEAD], url)

    def test_a_user_page_polls_for_its_author(self) -> None:
        create_test(self.author, approved=False)

        content = self.client.get('/user/author/').content.decode()

        self.assertIn('data-live-author="author"', content)
        self.assertIn('data-live-status="pending"', content)

    def test_an_unfinished_workload_page_polls_and_offers_the_notification(self) -> None:
        test = create_test(self.author, games=8)

        content = self.client.get(f'/test/{test.id}/').content.decode()

        self.assertIn('live.js', content)
        self.assertIn(f'data-live-workload="/api/live/workload/{test.id}/"', content)
        self.assertIn('data-live-status="active" data-live-games="8"', content)
        self.assertIn('data-live-notify aria-pressed="false" hidden', content)

    def test_a_finished_workload_page_does_not_poll(self) -> None:
        test = create_test(self.author, finished=True, passed=True)

        content = self.client.get(f'/test/{test.id}/').content.decode()

        self.assertNotIn('live.js', content)
        self.assertNotIn('data-live-workload', content)
        self.assertNotIn('data-live-notify', content)


class FreshnessTests(LiveCase):
    def test_an_idle_listing_is_resent_once_per_window(self) -> None:
        create_test(self.author)
        now = timezone.now()
        token = live_listing(None, None, now).token
        within = now.replace(second=0, microsecond=0) + timedelta(seconds=59)
        beyond = within + timedelta(seconds=1) + FRESHNESS

        self.assertIsInstance(live_listing(None, token, now), Unchanged)
        self.assertIsInstance(live_listing(None, token, beyond), LiveListing)
