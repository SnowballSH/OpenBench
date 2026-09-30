from unittest import mock

from django.contrib.sessions.models import Session
from django.test import TestCase

from OpenBench.config import OPENBENCH_CONFIG
from OpenBench.tests.fixtures import create_engine_config, create_test, create_user, ensure_book, register_payload

PAGES = ('/', '/index/', '/index/2/', '/greens/', '/users/', '/machines/', '/progress/', '/Ethereal/', '/nowhere/')


class LoginRequiredTests(TestCase):
    def setUp(self):
        create_engine_config()
        ensure_book()
        self.user = create_user('reader')
        self.test = create_test(self.user)

    def test_anonymous_pages_redirect_before_the_view_runs(self):
        for url in (*PAGES, f'/test/{self.test.id}/'):
            with self.assertNumQueries(0):
                response = self.client.get(url)
            self.assertRedirects(response, '/login/', fetch_redirect_response=False, msg_prefix=url)
            self.assertEqual(self.client.head(url).status_code, 302, url)
        self.assertFalse(Session.objects.exists())
        self.assertNotIn('sessionid', self.client.cookies)

    def test_login_page_stores_no_session(self):
        response = self.client.get('/login/')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Session.objects.exists())

    def test_public_paths_are_left_to_their_views(self):
        expected = {
            '/login/': 200,
            '/register/': 200,
            '/logout/': 302,
            '/health/': 200,
            '/api/config/': 401,
            '/clientGetBuildInfo/': 200,
            '/admin/': 302,
        }
        with mock.patch.dict(OPENBENCH_CONFIG, {'require_manual_registration': False}):
            for url, status in expected.items():
                response = self.client.get(url)
                self.assertEqual(response.status_code, status, url)
                self.assertNotEqual(response.get('Location'), '/login/', url)

    def test_client_endpoints_still_work(self):
        response = self.client.post('/clientWorkerInfo/', register_payload(self.user))
        self.assertIn('machine_id', response.json())

    def test_logged_in_users_see_pages(self):
        self.client.force_login(self.user)
        for url in PAGES[:-2]:
            self.assertEqual(self.client.get(url).status_code, 200, url)

    def test_public_servers_are_unaffected(self):
        with mock.patch.dict(OPENBENCH_CONFIG, {'require_login_to_view': False}):
            self.assertEqual(self.client.get('/index/').status_code, 200)


class AnonymousSessionTests(TestCase):
    def assert_no_session(self):
        self.assertFalse(Session.objects.exists())
        self.assertNotIn('sessionid', self.client.cookies)

    def test_closed_registration_redirects_without_a_session(self):
        with mock.patch.dict(OPENBENCH_CONFIG, {'require_manual_registration': True}):
            response = self.client.get('/register/')
        self.assertRedirects(response, '/login/', fetch_redirect_response=False)
        self.assert_no_session()

    def test_scripts_get_redirects_without_a_session(self):
        response = self.client.get('/scripts/')
        self.assertRedirects(response, '/login/', fetch_redirect_response=False)
        self.assert_no_session()
