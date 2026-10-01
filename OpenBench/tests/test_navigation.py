from dataclasses import dataclass, field
from typing import Any
from unittest import mock

from django.contrib.sessions.models import Session
from django.test import TestCase

from OpenBench.config import OPENBENCH_CONFIG
from OpenBench.models import Machine, Test
from OpenBench.navigation.catalogue import DatabaseCatalogue
from OpenBench.navigation.query import MAX_QUERY_LENGTH
from OpenBench.navigation.resolve import TOO_LONG, Jump, WorkloadRef, resolve
from OpenBench.navigation.suggest import MAX_SUGGESTIONS, suggestions
from OpenBench.tests.fixtures import create_engine_config, create_test, create_user, ensure_book, present, system_info

SHA_A = '76f2da3c' + 'a' * 32
SHA_B = '76f2da3c' + 'b' * 32
SHA_C = '874c026d' + 'c' * 32


def ref(workload_id: int, kind: str = 'test') -> WorkloadRef:
    return WorkloadRef(id=workload_id, kind=kind, title=f'title {workload_id}', detail='8.0+0.08')


@dataclass
class FakeCatalogue:
    workloads: dict[int, WorkloadRef] = field(default_factory=dict)
    commits: dict[str, list[WorkloadRef]] = field(default_factory=dict)
    users: tuple[str, ...] = ()
    engines: tuple[str, ...] = ()
    machines: frozenset[int] = frozenset()
    calls: list[str] = field(default_factory=list)

    def workload(self, workload_id: int) -> WorkloadRef | None:
        self.calls.append('workload')
        return self.workloads.get(workload_id)

    def commit_workloads(self, prefix: str, limit: int) -> list[WorkloadRef]:
        self.calls.append('commit')
        return [found for sha, refs in self.commits.items() if sha.startswith(prefix) for found in refs][:limit]

    def text_workloads(self, text: str, limit: int) -> list[WorkloadRef]:
        self.calls.append('text')
        return [found for found in self.workloads.values() if text in found.title][:limit]

    def username(self, name: str) -> str | None:
        self.calls.append('username')
        return next((user for user in self.users if user.lower() == name.lower()), None)

    def engine(self, name: str) -> str | None:
        self.calls.append('engine')
        return next((engine for engine in self.engines if engine.lower() == name.lower()), None)

    def machine_exists(self, machine_id: int) -> bool:
        self.calls.append('machine')
        return machine_id in self.machines


class ResolveTests(TestCase):
    def setUp(self) -> None:
        self.catalogue = FakeCatalogue(
            workloads={12: ref(12), 13: ref(13, 'tune'), 14: ref(14, 'datagen'), 1234567: ref(1234567)},
            commits={SHA_A: [ref(7)], SHA_B: [ref(8)], SHA_C: [ref(9)], '7654321' + 'f' * 33: [ref(5)]},
            users=('Admin', 'lab-worker', 'deadbeef'),
            engines=('Avalanche', 'Stock/Fish'),
            machines=frozenset({12}),
        )

    def jump(self, raw: str) -> Jump:
        return resolve(raw, self.catalogue)

    def test_ids_open_the_workload_under_its_type(self) -> None:
        self.assertEqual(self.jump('#12'), Jump('/test/12/'))
        self.assertEqual(self.jump(' 12 '), Jump('/test/12/'))
        self.assertEqual(self.jump('0012'), Jump('/test/12/'))
        self.assertEqual(self.jump('13'), Jump('/tune/13/'))
        self.assertEqual(self.jump('#14'), Jump('/datagen/14/'))

    def test_unknown_id_goes_to_search_with_a_notice(self) -> None:
        self.assertEqual(self.jump('99'), Jump('/search/', notice='No workload #99'))
        self.assertEqual(self.jump('#7654321'), Jump('/search/', notice='No workload #7654321'))

    def test_ids_are_ascii_and_bounded(self) -> None:
        self.assertEqual(self.jump('٣'), Jump('/search/?q=%D9%A3'))
        self.assertEqual(self.jump('#' + '9' * 19), Jump('/search/?q=%23' + '9' * 19))
        self.assertEqual(self.jump('#' + '9' * 18), Jump('/search/', notice=f'No workload #{"9" * 18}'))

    def test_long_digit_runs_fall_back_to_commits(self) -> None:
        self.assertEqual(self.jump('1234567'), Jump('/test/1234567/'))
        self.assertEqual(self.jump('7654321'), Jump('/test/5/'))
        self.assertEqual(self.jump('9' * 19), Jump('/search/?q=' + '9' * 19))

    def test_a_unique_commit_prefix_opens_its_workload(self) -> None:
        self.assertEqual(self.jump('874c026'), Jump('/test/9/'))
        self.assertEqual(self.jump('874C026D'), Jump('/test/9/'))
        self.assertEqual(self.jump(SHA_A), Jump('/test/7/'))

    def test_an_ambiguous_commit_prefix_lists_the_matches(self) -> None:
        self.assertEqual(self.jump('76f2da3c'), Jump('/search/?q=76f2da3c'))

    def test_an_unknown_commit_prefix_searches_text(self) -> None:
        self.assertEqual(self.jump('abcdef0'), Jump('/search/?q=abcdef0'))

    def test_short_hex_is_not_a_commit(self) -> None:
        self.assertEqual(self.jump('874c02'), Jump('/search/?q=874c02'))
        self.assertNotIn('commit', self.catalogue.calls)

    def test_hex_that_names_a_user_still_resolves(self) -> None:
        self.assertEqual(self.jump('deadbeef'), Jump('/user/deadbeef/'))

    def test_users(self) -> None:
        self.assertEqual(self.jump('user:admin'), Jump('/user/Admin/'))
        self.assertEqual(self.jump('USER: lab-worker'), Jump('/user/lab-worker/'))
        self.assertEqual(self.jump('admin'), Jump('/user/Admin/'))
        self.assertEqual(self.jump('user:nobody'), Jump('/users/', notice='No user named nobody'))
        self.assertEqual(self.jump('user:a/b'), Jump('/users/', notice='No user named a/b'))

    def test_engines(self) -> None:
        self.assertEqual(self.jump('avalanche'), Jump('/progress/Avalanche/?window=90d'))
        self.assertEqual(self.jump('stock/fish'), Jump('/progress/?engine=Stock%2FFish&window=90d'))

    def test_machines(self) -> None:
        for raw in ('machine 12', 'Machine12', 'm:12', 'M#12', 'machine:12', 'machine #12'):
            self.assertEqual(self.jump(raw), Jump('/machines/12/'), raw)

    def test_a_cpu_model_is_not_a_machine_id(self) -> None:
        self.assertEqual(self.jump('m12'), Jump('/search/?q=m12'))
        self.assertEqual(self.jump('M 12'), Jump('/search/?q=M+12'))
        self.assertNotIn('machine', self.catalogue.calls)

    def test_an_unknown_machine_searches_text(self) -> None:
        self.assertEqual(self.jump('machine 13'), Jump('/search/?q=machine+13'))
        self.assertEqual(self.jump('m:13'), Jump('/search/?q=m%3A13'))

    def test_dot_names_never_become_a_user_path(self) -> None:
        catalogue = FakeCatalogue(users=('.', '..', '...'))
        for name in ('.', '..', '...'):
            self.assertEqual(resolve(name, catalogue), Jump(f'/search/?q={name}'))
            self.assertEqual(resolve(f'user:{name}', catalogue), Jump('/users/', notice=f'No user named {name}'))
            self.assertEqual([entry.url for entry in suggestions(name, catalogue)][0][:8], '/search/')
        self.assertNotIn('username', catalogue.calls)

    def test_anything_else_searches_text(self) -> None:
        self.assertEqual(self.jump('pawn  corrhist'), Jump('/search/?q=pawn+corrhist'))
        self.assertEqual(self.jump('a&b=c#d'), Jump('/search/?q=a%26b%3Dc%23d'))

    def test_empty_and_oversized_input(self) -> None:
        self.assertEqual(self.jump('   '), Jump('/search/'))
        self.assertEqual(self.jump('x' * (MAX_QUERY_LENGTH + 1)), Jump('/search/', notice=TOO_LONG))
        self.assertEqual(self.catalogue.calls, [])
        self.assertEqual(self.jump('x' * MAX_QUERY_LENGTH).notice, None)

    def test_every_destination_is_an_internal_path(self) -> None:
        hostile = ('//evil.example', 'https://evil.example/', '/\\evil.example', 'user://evil.example', '..', '.', '\\')
        catalogue = FakeCatalogue(users=hostile, engines=hostile)
        for raw in (*hostile, *(f'user:{name}' for name in hostile)):
            path = resolve(raw, catalogue).path
            self.assertRegex(path, r'^/(search|users|user|progress)/', raw)
            self.assertNotRegex(path, r'^/[/\\]', raw)


class SuggestionTests(TestCase):
    def test_direct_hits_come_first_and_search_last(self) -> None:
        catalogue = FakeCatalogue(workloads={12: ref(12)}, users=('12',), machines=frozenset({12}))
        found = suggestions('12', catalogue)
        self.assertEqual([entry.url for entry in found], ['/test/12/', '/user/12/', '/search/?q=12'])
        self.assertEqual(found[0].label, '#12 title 12')

    def test_results_are_capped_and_unique(self) -> None:
        catalogue = FakeCatalogue(workloads={index: ref(index) for index in range(1, 30)})
        found = suggestions('title', catalogue)
        self.assertEqual(len(found), MAX_SUGGESTIONS)
        self.assertEqual(found[-1].url, '/search/?q=title')
        self.assertEqual(len({entry.url for entry in found}), len(found))

    def test_deleted_workloads_are_not_suggested(self) -> None:
        gone = WorkloadRef(id=12, kind='test', title='gone', detail='', deleted=True)
        catalogue = FakeCatalogue(workloads={12: gone})
        self.assertEqual([entry.url for entry in suggestions('#12', catalogue)], ['/search/?q=%2312'])
        self.assertEqual(resolve('#12', catalogue), Jump('/test/12/'))

    def test_one_character_runs_no_text_search(self) -> None:
        catalogue = FakeCatalogue()
        self.assertEqual([entry.url for entry in suggestions('x', catalogue)], ['/search/?q=x'])
        self.assertNotIn('text', catalogue.calls)

    def test_empty_and_oversized_input_suggest_nothing(self) -> None:
        catalogue = FakeCatalogue()
        self.assertEqual(suggestions(' ', catalogue), [])
        self.assertEqual(suggestions('x' * (MAX_QUERY_LENGTH + 1), catalogue), [])
        self.assertEqual(catalogue.calls, [])


class PinnedWorkloads(TestCase):
    def setUp(self) -> None:
        create_engine_config()
        ensure_book()
        self.user = create_user('reader')
        self.client.force_login(self.user)
        self.first = self.pinned(SHA_A, SHA_C, info='Pawn corrhist (corrhist-pawn), STC\navl:6b10ec947ac0')
        self.second = self.pinned(SHA_B, SHA_A, info='Pawn corrhist, LTC confirmation of #1')
        self.datagen = self.pinned('d' * 40, 'e' * 40, test_mode='DATAGEN')

    def pinned(self, dev_sha: str, base_sha: str, **fields: Any) -> Test:
        test = create_test(self.user, **fields)
        for engine, sha in ((test.dev, dev_sha), (test.base, base_sha)):
            engine.name = engine.sha = sha
            engine.save()
        return test


class DatabaseCatalogueTests(PinnedWorkloads):
    def test_workload_carries_its_type_and_label(self) -> None:
        catalogue = DatabaseCatalogue()
        self.assertEqual(
            catalogue.workload(self.first.id),
            WorkloadRef(
                id=self.first.id,
                kind='test',
                title='Pawn corrhist (corrhist-pawn), STC',
                detail='76f2da3c vs 874c026d · 8.0+0.08',
            ),
        )
        datagen = present(catalogue.workload(self.datagen.id))
        self.assertEqual((datagen.kind, datagen.title, datagen.detail), ('datagen', 'dddddddd vs eeeeeeee', '8.0+0.08'))
        self.assertIsNone(catalogue.workload(10**17))

    def test_commit_prefix_matches_dev_or_base_newest_first(self) -> None:
        catalogue = DatabaseCatalogue()
        self.assertEqual([found.id for found in catalogue.commit_workloads('874c026', 5)], [self.first.id])
        self.assertEqual(
            [found.id for found in catalogue.commit_workloads('76f2da3c', 5)], [self.second.id, self.first.id]
        )
        self.assertEqual(
            [found.id for found in catalogue.commit_workloads('76f2da3ca', 5)], [self.second.id, self.first.id]
        )
        self.assertEqual([found.id for found in catalogue.commit_workloads('76f2da3cb', 5)], [self.second.id])
        self.assertEqual(len(catalogue.commit_workloads('76f2da3c', 1)), 1)

    def test_commit_prefix_matches_a_branch_name_too(self) -> None:
        self.datagen.dev.name = 'abcdef12-branch'
        self.datagen.dev.save()
        self.assertEqual([found.id for found in DatabaseCatalogue().commit_workloads('abcdef12', 5)], [self.datagen.id])

    def test_deleted_workloads_are_not_listed(self) -> None:
        Test.objects.filter(id=self.second.id).update(deleted=True)
        catalogue = DatabaseCatalogue()
        self.assertEqual([found.id for found in catalogue.commit_workloads('76f2da3c', 5)], [self.first.id])
        self.assertEqual([found.id for found in catalogue.text_workloads('pawn', 5)], [self.first.id])
        self.assertIsNotNone(catalogue.workload(self.second.id))

    def test_names_resolve_to_their_stored_spelling(self) -> None:
        machine = Machine.objects.create(user=self.user, info=system_info())
        catalogue = DatabaseCatalogue()
        self.assertEqual(catalogue.username('READER'), 'reader')
        self.assertIsNone(catalogue.username('nobody'))
        self.assertEqual(catalogue.engine('avalanche'), 'Avalanche')
        self.assertIsNone(catalogue.engine('Stockfish'))
        self.assertTrue(catalogue.machine_exists(machine.id))
        self.assertFalse(catalogue.machine_exists(machine.id + 1))

    def test_each_lookup_is_one_query(self) -> None:
        catalogue = DatabaseCatalogue()
        lookups = (
            lambda: catalogue.workload(self.first.id),
            lambda: catalogue.commit_workloads('76f2da3c', 5),
            lambda: catalogue.text_workloads('pawn corrhist', 5),
            lambda: catalogue.username('reader'),
            lambda: catalogue.engine('Avalanche'),
            lambda: catalogue.machine_exists(1),
        )
        for lookup in lookups:
            with self.assertNumQueries(1):
                lookup()


class GoViewTests(PinnedWorkloads):
    def assert_goes(self, query: str, destination: str) -> None:
        response = self.client.get('/go/', {'q': query})
        self.assertRedirects(response, destination, fetch_redirect_response=False, msg_prefix=query)

    def test_redirect_targets(self) -> None:
        machine = Machine.objects.create(user=self.user, info=system_info())
        self.assert_goes(f'#{self.first.id}', f'/test/{self.first.id}/')
        self.assert_goes(str(self.datagen.id), f'/datagen/{self.datagen.id}/')
        self.assert_goes('874c026', f'/test/{self.first.id}/')
        self.assert_goes('76f2da3cb', f'/test/{self.second.id}/')
        self.assert_goes('76f2da3c', '/search/?q=76f2da3c')
        self.assert_goes('user:Reader', '/user/reader/')
        self.assert_goes('reader', '/user/reader/')
        self.assert_goes('avalanche', '/progress/Avalanche/?window=90d')
        self.assert_goes(f'm:{machine.id}', f'/machines/{machine.id}/')
        self.assert_goes(f'm{machine.id}', f'/search/?q=m{machine.id}')
        self.assert_goes('corrhist', '/search/?q=corrhist')
        self.assert_goes('', '/search/')

    def test_ambiguous_commit_lists_the_matches_newest_first(self) -> None:
        response = self.client.get('/go/', {'q': '76f2da3c'}, follow=True)
        content = response.content.decode()
        first, second = (content.index(f'href="/test/{test.id}/"') for test in (self.first, self.second))
        self.assertLess(second, first)
        self.assertIn(f'data-row-href="/test/{self.first.id}/"', content)
        self.assertNotIn(f'/datagen/{self.datagen.id}/', content)

    def test_notices_show_on_the_landing_page(self) -> None:
        for query, notice in (
            ('#999', 'No workload #999'),
            ('user:nobody', 'No user named nobody'),
            ('x' * (MAX_QUERY_LENGTH + 1), TOO_LONG),
            ('zzzz-nothing', 'No matching tests found'),
        ):
            response = self.client.get('/go/', {'q': query}, follow=True)
            self.assertEqual(response.status_code, 200, query)
            self.assertContains(response, notice)

    def test_head_answers_like_get_and_post_is_refused(self) -> None:
        self.assertEqual(self.client.head('/go/', {'q': '1'}).status_code, 302)
        self.assertEqual(self.client.post('/go/', {'q': '1'}).status_code, 405)

    def test_anonymous_is_sent_to_login_before_any_query(self) -> None:
        self.client.logout()
        Session.objects.all().delete()
        with self.assertNumQueries(0):
            response = self.client.get('/go/', {'q': f'#{self.first.id}'})
        self.assertRedirects(response, '/login/', fetch_redirect_response=False)

    def test_query_count_does_not_grow_with_workloads(self) -> None:
        def assert_budget() -> None:
            for query, queries in (('76f2da3c', 3), ('1234567', 6), ('nothing-matches', 4)):
                with self.subTest(query=query), self.assertNumQueries(queries):
                    self.client.get('/go/', {'q': query})

        assert_budget()
        for _ in range(20):
            self.pinned(SHA_A, SHA_B, info='more')
        assert_budget()


class HeaderTests(PinnedWorkloads):
    def test_header_carries_the_jump_form(self) -> None:
        content = self.client.get('/index/').content.decode()
        self.assertIn('<form id="quick-jump" class="quick-jump" method="get" action="/go/"', content)
        self.assertIn('<label class="visually-hidden" for="quick-jump-input">', content)
        self.assertIn('placeholder="Jump to #id, commit or text"', content)

    def test_login_page_has_no_jump_form(self) -> None:
        self.client.logout()
        self.assertNotContains(self.client.get('/login/'), 'quick-jump-input')

    def test_public_servers_offer_it_to_anonymous_viewers(self) -> None:
        self.client.logout()
        with mock.patch.dict(OPENBENCH_CONFIG, {'require_login_to_view': False}):
            self.assertContains(self.client.get('/index/'), 'quick-jump-input')
            response = self.client.get('/go/', {'q': f'#{self.first.id}'})
        self.assertRedirects(response, f'/test/{self.first.id}/', fetch_redirect_response=False)

    def test_a_notice_opens_no_session_for_an_anonymous_viewer(self) -> None:
        self.client.logout()
        Session.objects.all().delete()
        with mock.patch.dict(OPENBENCH_CONFIG, {'require_login_to_view': False}):
            response = self.client.get('/go/', {'q': '#999'})
        self.assertRedirects(response, '/search/', fetch_redirect_response=False)
        self.assertFalse(Session.objects.exists())
        self.assertNotIn('sessionid', response.cookies)


class SearchTextTests(PinnedWorkloads):
    def shown(self, query: str) -> list[int]:
        response = self.client.get(f'/search/?{query}')
        return [test.id for test in response.context.get('tests', [])]

    def test_text_matches_info_names_and_commits(self) -> None:
        both = [self.second.id, self.first.id]
        self.assertEqual(self.shown('q=PAWN'), both)
        self.assertEqual(self.shown('q=76f2da3c'), both)
        self.assertEqual(self.shown('q=874c026'), [self.first.id])
        self.assertEqual(self.shown('q=6b10ec947ac0'), [self.first.id])
        self.assertEqual(self.shown('q=dddd'), [self.datagen.id])

    def test_short_terms_do_not_match_inside_commit_names(self) -> None:
        for term in ('3c', 'da3', 'aaa', 'f2', '26d'):
            self.assertEqual(self.shown(f'q={term}'), [], term)
            urls = [entry['url'] for entry in self.client.get('/api/jump/', {'q': term}).json()['suggestions']]
            self.assertEqual(urls, [f'/search/?q={term}'], term)

    def test_terms_still_match_inside_branch_names(self) -> None:
        for engine, name in ((self.datagen.dev, 'corrhist-add'), (self.datagen.base, 'deadbeef')):
            engine.name = name
            engine.save()
        self.assertEqual(self.shown('q=add'), [self.datagen.id])
        self.assertEqual(self.shown('q=ADBE'), [self.datagen.id])

    def test_every_term_must_match(self) -> None:
        self.assertEqual(self.shown('q=pawn+ltc'), [self.second.id])
        self.assertEqual(self.shown('q=pawn+76f2da3cb'), [self.second.id])
        self.assertEqual(self.shown('q=pawn+nothing'), [])

    def test_text_combines_with_the_other_filters(self) -> None:
        self.assertEqual(self.shown('q=pawn&info-contains=STC'), [self.first.id])
        self.assertEqual(self.shown('keywords=da3ca'), [self.first.id])
        self.assertEqual(self.shown('q=pawn&workload-type=SPSA'), [])

    def test_text_is_echoed_and_paged(self) -> None:
        for _ in range(30):
            create_test(self.user, info='pawn again')
        content = self.client.get('/search/?q=pawn').content.decode()
        self.assertIn('name="q" value="pawn"', content)
        self.assertIn('href="/search/2/?q=pawn"', content)
        self.assertEqual(len(self.shown('q=pawn')), 25)

    def test_control_characters_separate_terms_and_match_nothing_alone(self) -> None:
        self.assertEqual(self.shown('q=%00'), self.shown('q='))
        self.assertEqual(self.shown('q=%00nothing'), [])
        self.assertEqual(self.shown('q=pawn%00ltc'), [self.second.id])

    def test_oversized_text_is_refused(self) -> None:
        response = self.client.get('/search/', {'q': 'x' * (MAX_QUERY_LENGTH + 1)})
        self.assertContains(response, f'Search at most {MAX_QUERY_LENGTH} characters of text')
        self.assertNotIn('tests', response.context)


class ApiJumpTests(PinnedWorkloads):
    def test_suggestions(self) -> None:
        payload = self.client.get('/api/jump/', {'q': '76f2da3c'}).json()
        self.assertEqual(
            payload['suggestions'],
            [
                {
                    'label': f'#{self.second.id} Pawn corrhist, LTC confirmation of #1',
                    'detail': '76f2da3c vs 76f2da3c · 8.0+0.08',
                    'url': f'/test/{self.second.id}/',
                },
                {
                    'label': f'#{self.first.id} Pawn corrhist (corrhist-pawn), STC',
                    'detail': '76f2da3c vs 874c026d · 8.0+0.08',
                    'url': f'/test/{self.first.id}/',
                },
                {'label': 'Search for “76f2da3c”', 'detail': 'Search', 'url': '/search/?q=76f2da3c'},
            ],
        )

    def test_id_user_and_engine_hits(self) -> None:
        urls = [
            entry['url'] for entry in self.client.get('/api/jump/', {'q': f'#{self.datagen.id}'}).json()['suggestions']
        ]
        self.assertEqual(urls[0], f'/datagen/{self.datagen.id}/')
        urls = [entry['url'] for entry in self.client.get('/api/jump/', {'q': 'reader'}).json()['suggestions']]
        self.assertEqual(urls[0], '/user/reader/')
        urls = [entry['url'] for entry in self.client.get('/api/jump/', {'q': 'avalanche'}).json()['suggestions']]
        self.assertEqual(urls[0], '/progress/Avalanche/?window=90d')

    def test_results_stay_bounded(self) -> None:
        for _ in range(20):
            create_test(self.user, info='pawn again')
        with self.assertNumQueries(6):
            payload = self.client.get('/api/jump/', {'q': 'pawn'}).json()
        self.assertEqual(len(payload['suggestions']), MAX_SUGGESTIONS)

    def test_deleted_workloads_are_left_out(self) -> None:
        Test.objects.filter(id=self.first.id).update(deleted=True)
        urls = [
            entry['url'] for entry in self.client.get('/api/jump/', {'q': f'#{self.first.id}'}).json()['suggestions']
        ]
        self.assertNotIn(f'/test/{self.first.id}/', urls)
        self.assertRedirects(
            self.client.get('/go/', {'q': f'#{self.first.id}'}),
            f'/test/{self.first.id}/',
            fetch_redirect_response=False,
        )

    def test_empty_query(self) -> None:
        self.assertEqual(self.client.get('/api/jump/').json(), {'suggestions': []})

    def test_anonymous_is_refused(self) -> None:
        self.client.logout()
        response = self.client.get('/api/jump/', {'q': 'pawn'})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json(), {'error': 'API requires authentication for this server'})

    def test_disabled_accounts_are_refused(self) -> None:
        self.client.force_login(create_user('pending', enabled=False))
        self.assertEqual(self.client.get('/api/jump/', {'q': 'pawn'}).status_code, 401)
