from unittest import mock

from django.contrib.auth.models import AnonymousUser
from django.contrib.sessions.backends.db import SessionStore
from django.contrib.sessions.models import Session
from django.template.loader import render_to_string
from django.test import RequestFactory, TestCase, override_settings
from django.urls import get_resolver

from OpenBench.config import OPENBENCH_CONFIG
from OpenBench.security import error_pages
from OpenBench.tests.fixtures import create_engine_config, create_user
from OpenBench.tests.test_accessibility import audit
from OpenBench.tests.test_csp import inline_code


@override_settings(DEBUG=False)
class NotFoundPageTests(TestCase):
    def setUp(self):
        create_engine_config('SecretEngine')
        self.user = create_user('reader')

    def assert_styled_not_found(self, url):
        response = self.client.get(url)
        self.assertEqual(response.status_code, 404)
        content = response.content.decode()
        self.assertIn('<title>Not found · OpenBench</title>', content)
        self.assertIn('<a class="skip-link" href="#content">', content)
        self.assertEqual(inline_code(content), [])
        return content

    def test_unknown_path_renders_the_site_page(self):
        self.client.force_login(self.user)
        self.assert_styled_not_found('/nonexistent/')

    def test_unknown_progress_engine_renders_the_site_page(self):
        self.client.force_login(self.user)
        self.assert_styled_not_found('/progress/Nope/')

    def test_page_is_accessible(self):
        self.client.force_login(self.user)
        result = audit(self.assert_styled_not_found('/nonexistent/'))
        self.assertEqual(result.html_lang, 'en')
        self.assertEqual(result.landmarks['main'], 1)
        self.assertEqual(result.headings.count(1), 1)
        self.assertEqual(result.skipped_heading_levels(), [])
        self.assertEqual(result.nameless(), [])

    def test_anonymous_page_leaks_nothing_and_stores_no_session(self):
        with mock.patch.dict(OPENBENCH_CONFIG, {'require_login_to_view': False}), self.assertNumQueries(0):
            content = self.assert_styled_not_found('/nonexistent/secret-path/')
        self.assertNotIn('SecretEngine', content)
        self.assertNotIn('secret-path', content)
        self.assertFalse(Session.objects.exists())


class ServerErrorPageTests(TestCase):
    def test_template_renders_without_context_or_database(self):
        with self.assertNumQueries(0):
            content = render_to_string('OpenBench/500.html')
        self.assertIn('<title>Server error · OpenBench</title>', content)
        self.assertEqual(inline_code(content), [])

    def test_handler_renders_without_database(self):
        with self.assertNumQueries(0):
            response = error_pages.server_error(RequestFactory().get('/'))
        self.assertEqual(response.status_code, 500)
        result = audit(response.content.decode())
        self.assertEqual(result.html_lang, 'en')
        self.assertEqual(result.landmarks['main'], 1)
        self.assertEqual(result.headings.count(1), 1)
        self.assertEqual(result.nameless(), [])

    def test_handlers_are_installed(self):
        resolver = get_resolver()
        self.assertIs(resolver.resolve_error_handler(404), error_pages.page_not_found)
        self.assertIs(resolver.resolve_error_handler(403), error_pages.permission_denied)
        self.assertIs(resolver.resolve_error_handler(500), error_pages.server_error)


@override_settings(DEBUG=False)
class ForbiddenPageTests(TestCase):
    def test_permission_denied_renders_the_site_page(self):
        request = RequestFactory().get('/')
        request.user = AnonymousUser()
        request.session = SessionStore()
        with self.assertNumQueries(0):
            response = error_pages.permission_denied(request, PermissionError('secret reason'))
        self.assertEqual(response.status_code, 403)
        content = response.content.decode()
        self.assertIn('<title>Forbidden · OpenBench</title>', content)
        self.assertNotIn('secret reason', content)
        self.assertEqual(inline_code(content), [])
