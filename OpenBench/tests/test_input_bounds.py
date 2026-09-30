from django.test import TestCase
from django.urls import Resolver404, resolve

from OpenBench.tests.fixtures import create_engine_config, create_test, create_user, ensure_book

LONG_PAGES = ('5' * 5000, '9' * 11)
LONG_IDS = ('5' * 5000, '9' * 19)


class SearchTermLimitTests(TestCase):
    def setUp(self):
        create_engine_config()
        ensure_book()
        self.user = create_user('reader')
        create_test(self.user)
        self.client.force_login(self.user)

    def test_many_keywords_or_authors_return_the_form_with_an_error(self):
        many = ' '.join(f'term{index}' for index in range(5000))
        for field in ('keywords', 'authors'):
            response = self.client.get('/search/', {field: many})
            self.assertEqual(response.status_code, 200, field)
            self.assertEqual(response.context['form'][field], many)
            self.assertContains(response, f'Search at most 20 {field}')

    def test_twenty_terms_still_search(self):
        response = self.client.get('/search/', {'keywords': ' '.join(['dev'] * 20)})
        self.assertEqual(len(response.context['tests']), 1)


class PageNumberLimitTests(TestCase):
    def setUp(self):
        self.user = create_user('reader')
        self.client.force_login(self.user)

    def test_oversized_numbers_are_not_found(self):
        pages = [
            f'/{prefix}/{number}/'
            for prefix in ('index', 'greens', 'search', 'events', 'errors', 'user/reader')
            for number in LONG_PAGES
        ]
        ids = [f'/{prefix}/{number}/' for prefix in ('machines', 'test', 'tune', 'datagen') for number in LONG_IDS]
        for url in pages + ids:
            self.assertEqual(self.client.get(url).status_code, 404, url[:40])

    def test_ordinary_pages_still_resolve(self):
        for url in ('/index/1/', '/greens/9999999999/', '/events/2/', '/user/reader/3/'):
            self.assertEqual(self.client.get(url).status_code, 200, url)


class IdConverterTests(TestCase):
    def setUp(self):
        self.client.force_login(create_user('reader'))

    def test_oversized_ids_do_not_resolve(self):
        for number in LONG_IDS:
            for url in (
                f'/event/{number}/',
                f'/api/pgns/{number}/',
                f'/api/spsa/{number}/inputs/',
                f'/api/workload/{number}/info/',
            ):
                with self.subTest(url=url[:40]):
                    with self.assertRaises(Resolver404):
                        resolve(url)
                    self.assertEqual(self.client.get(url).status_code, 404)

    def test_ids_still_resolve_as_integers(self):
        self.assertEqual(resolve('/event/12/').kwargs, {'pk': 12})
        self.assertEqual(resolve(f'/api/workload/{"9" * 18}/info/').kwargs['workload_id'], int('9' * 18))
