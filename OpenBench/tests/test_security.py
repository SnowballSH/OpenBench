import copy
import json
import tempfile
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.contrib.sessions.middleware import SessionMiddleware
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.http import HttpResponse
from django.test import RequestFactory, TestCase, override_settings

import OpenBench.views
from OpenBench.config import verify_general_config
from OpenBench.models import PGN, LogEvent, Machine, Network, Result, Test
from OpenBench.security import throttle
from OpenBench.tests.fixtures import (
    PASSWORD,
    create_engine_config,
    create_test,
    create_user,
    credentials,
    ensure_book,
    register_payload,
)
from OpenBench.upstream import openbench_config


def clear_throttle(test_case):
    cache.clear()
    test_case.addCleanup(cache.clear)


class EngineOptionsPopupTests(TestCase):
    def setUp(self):
        create_engine_config()
        ensure_book()
        self.user = create_user('reader')
        self.client.force_login(self.user)

    def test_options_render_escaped_and_popup_uses_text_nodes(self):
        payload = '<img/src=x/onerror=alert(1)>'
        test = create_test(self.user, dev_options=f'Threads=1 Hash=16 {payload}')

        content = self.client.get(f'/test/{test.id}/').content.decode()

        self.assertNotIn(payload, content)
        self.assertIn('&lt;img/src=x/onerror=alert(1)&gt;', content)

        site_js = (Path(settings.BASE_DIR) / 'OpenBench' / 'static' / 'site.js').read_text()
        self.assertNotIn('innerHTML', site_js)
        self.assertIn('createTextNode(option)', site_js)


class LogoutTests(TestCase):
    def setUp(self):
        self.user = create_user('reader')
        self.client.force_login(self.user)

    def test_get_does_not_log_out(self):
        self.assertRedirects(self.client.get('/logout/'), '/index/', fetch_redirect_response=False)
        self.assertEqual(self.client.get('/index/').status_code, 200)

    def test_post_logs_out(self):
        self.assertRedirects(self.client.post('/logout/'), '/index/', fetch_redirect_response=False)
        self.assertRedirects(self.client.get('/index/'), '/login/', fetch_redirect_response=False)

    def test_sidebar_logs_out_with_a_csrf_protected_form(self):
        content = self.client.get('/index/').content.decode()
        self.assertRegex(
            content,
            r'<form id="logout-form" [^>]*method="post" action="/logout/"[^>]*><input [^>]*name="csrfmiddlewaretoken"',
        )
        self.assertIn('name="csrfmiddlewaretoken"', content)

    def test_post_without_csrf_token_is_refused(self):
        client = self.client_class(enforce_csrf_checks=True)
        client.force_login(self.user)
        self.assertEqual(client.post('/logout/').status_code, 403)
        self.assertEqual(client.get('/index/').status_code, 200)


class RegistrationTests(TestCase):
    def test_manual_registration_refuses_posts(self):
        response = self.client.post(
            '/register/',
            {
                'username': 'intruder',
                'email': '',
                'password1': PASSWORD,
                'password2': PASSWORD,
            },
        )
        self.assertRedirects(response, '/login/', fetch_redirect_response=False)
        self.assertFalse(Machine.objects.exists())
        self.assertFalse(self.client.session.get('_auth_user_id'))
        self.assertFalse(OpenBench.views.User.objects.filter(username='intruder').exists())

    def test_open_registration_still_logs_the_new_user_in(self):
        with mock.patch.dict(openbench_config(), {'require_manual_registration': False}):
            response = self.client.post(
                '/register/',
                {
                    'username': 'newcomer',
                    'email': '',
                    'password1': PASSWORD,
                    'password2': PASSWORD,
                },
            )
        self.assertRedirects(response, '/index/', fetch_redirect_response=False)
        self.assertTrue(self.client.session.get('_auth_user_id'))

    def test_password_change_keeps_the_session(self):
        self.client.force_login(create_user('reader'))
        response = self.client.post(
            '/profile/', {'email': '', 'password1': 'a-new-long-password', 'password2': 'a-new-long-password'}
        )
        self.assertRedirects(response, '/profile/', fetch_redirect_response=False)
        self.assertEqual(self.client.get('/index/').status_code, 200)


class ScriptsTests(TestCase):
    def setUp(self):
        clear_throttle(self)
        self.media = tempfile.TemporaryDirectory()
        self.enterContext(override_settings(MEDIA_ROOT=self.media.name))
        self.enterContext(mock.patch('OpenBench.utils.MEDIA_ROOT', self.media.name))
        self.addCleanup(self.media.cleanup)
        create_engine_config()
        self.approver = create_user('approver', approver=True)

    def upload(self, **auth):
        return self.client.post(
            '/scripts/',
            {
                **auth,
                'action': 'UPLOAD_NETWORK',
                'engine': 'Avalanche',
                'name': 'r1',
                'netfile': SimpleUploadedFile('net.nnue', b'weights'),
            },
        )

    def test_bad_credentials_do_not_fall_back_to_the_session(self):
        self.client.force_login(self.approver)
        response = self.upload(username='approver', password='wrong')
        self.assertRedirects(response, '/login/', fetch_redirect_response=False)
        self.assertFalse(Network.objects.exists())

    def test_missing_credentials_do_not_fall_back_to_the_session(self):
        self.client.force_login(self.approver)
        self.upload()
        self.assertFalse(Network.objects.exists())

    def test_valid_credentials_act_as_that_user(self):
        response = self.upload(**credentials(self.approver))
        self.assertRedirects(response, '/networks/Avalanche/', fetch_redirect_response=False)
        self.assertEqual(Network.objects.get().author, 'approver')

    def test_credentials_of_a_non_approver_cannot_upload(self):
        self.client.force_login(self.approver)
        self.upload(**credentials(create_user('worker')))
        self.assertFalse(Network.objects.exists())


class ApiNetworkDeleteTests(TestCase):
    def setUp(self):
        clear_throttle(self)
        create_engine_config()
        self.network = Network.objects.create(sha256='ABCDEF01', name='r1', engine='Avalanche', author='approver')

    def delete(self, user, **headers):
        return self.client.post('/api/networks/Avalanche/r1/delete/', credentials(user), headers=headers)

    def test_enabled_non_approver_cannot_delete(self):
        response = self.delete(create_user('lab-readonly'))
        self.assertIn('error', response.json())
        self.assertTrue(Network.objects.filter(id=self.network.id).exists())

    def test_approver_can_delete(self):
        self.assertIn('success', self.delete(create_user('admin', approver=True)).json())
        self.assertFalse(Network.objects.filter(id=self.network.id).exists())

    def test_cross_site_request_is_refused(self):
        response = self.delete(create_user('admin', approver=True), sec_fetch_site='same-site')
        self.assertEqual(response.status_code, 403)
        self.assertTrue(Network.objects.filter(id=self.network.id).exists())

    def test_get_is_refused(self):
        self.client.force_login(create_user('admin', approver=True))
        self.assertEqual(self.client.get('/api/networks/Avalanche/r1/delete/').status_code, 405)
        self.assertTrue(Network.objects.filter(id=self.network.id).exists())


class ApiAuthenticationLoggingTests(TestCase):
    def setUp(self):
        clear_throttle(self)
        create_engine_config()
        self.user = create_user('reader')

    def test_failure_logs_one_line_without_the_password(self):
        with self.assertLogs('OpenBench.views', 'WARNING') as logs:
            response = self.client.post('/api/config/', {'username': 'reader', 'password': 'hunter2-guess'})

        self.assertIn('error', json.loads(response.content))
        self.assertEqual(len(logs.output), 1)
        self.assertIn("'reader'", logs.output[0])
        self.assertNotIn('hunter2-guess', logs.output[0])
        self.assertNotIn('Traceback', logs.output[0])

    def test_valid_credentials_succeed_without_logging(self):
        with self.assertNoLogs('OpenBench.views', 'WARNING'):
            response = self.client.post('/api/config/', credentials(self.user))
        self.assertIn('engines', json.loads(response.content))


class WorkerOwnershipTests(TestCase):
    def setUp(self):
        self.media = tempfile.TemporaryDirectory()
        self.enterContext(override_settings(MEDIA_ROOT=self.media.name))
        self.addCleanup(self.media.cleanup)
        create_engine_config()
        ensure_book()
        admin = create_user('admin', approver=True)
        self.test = create_test(admin)
        self.owner = self.start_session(create_user('lab-worker'))
        self.thief = self.start_session(create_user('home-worker'))
        self.other = create_test(admin)

    def start_session(self, user):
        response = self.client.post('/clientWorkerInfo/', register_payload(user)).json()
        session = {'machine_id': response['machine_id'], 'secret': response['secret']}
        workload = self.client.post('/clientGetWorkload/', session).json()['workload']
        return {**session, 'result_id': workload['result']['id'], 'test_id': workload['test']['id']}

    def results(self, session, **overrides):
        return {
            **session,
            'crashes': 0,
            'timelosses': 0,
            'illegals': 0,
            'trinomial': '1 2 3',
            'pentanomial': '0 1 1 1 0',
            **overrides,
        }

    def test_both_sessions_were_given_the_same_test(self):
        self.assertEqual(self.owner['test_id'], self.thief['test_id'])
        self.assertNotEqual(self.owner['result_id'], self.thief['result_id'])

    def test_results_for_a_foreign_result_are_refused(self):
        forged = self.results(self.thief, result_id=self.owner['result_id'])
        response = self.client.post('/clientSubmitResults/', forged).json()

        self.assertEqual(response, {'stop': True})
        self.assertEqual(Result.objects.get(id=self.owner['result_id']).games, 0)
        self.assertEqual(Test.objects.get(id=self.test.id).games, 0)

    def test_results_for_a_mismatched_test_are_refused(self):
        forged = self.results(self.owner, test_id=self.other.id)
        self.assertEqual(self.client.post('/clientSubmitResults/', forged).json(), {'stop': True})
        self.assertEqual(Test.objects.get(id=self.other.id).games, 0)

    def test_negative_results_are_refused(self):
        forged = self.results(self.owner, trinomial='5 0 -5')
        self.assertEqual(self.client.post('/clientSubmitResults/', forged).json(), {'stop': True})
        self.assertEqual(Test.objects.get(id=self.test.id).games, 0)

    def test_own_results_are_accepted(self):
        self.assertEqual(self.client.post('/clientSubmitResults/', self.results(self.owner)).json(), {})
        self.assertEqual(Result.objects.get(id=self.owner['result_id']).games, 6)

    def test_nps_stats_for_a_foreign_result_are_refused(self):
        stats = {
            name: 100
            for name in ('dev_nodes', 'dev_time', 'dev_time_scaled', 'base_nodes', 'base_time', 'base_time_scaled')
        }
        forged = {**self.thief, **stats, 'result_id': self.owner['result_id']}
        response = self.client.post('/clientSubmitNPSStats/', forged).json()

        self.assertIn('error', response)
        self.assertEqual(Result.objects.get(id=self.owner['result_id']).dev_nodes, 0)
        self.assertEqual(self.client.post('/clientSubmitNPSStats/', {**self.owner, **stats}).json(), {})
        self.assertEqual(Result.objects.get(id=self.owner['result_id']).dev_nodes, 100)

    def test_bench_error_for_an_unassigned_test_is_refused(self):
        response = self.client.post('/clientBenchError/', {**self.thief, 'test_id': self.other.id, 'error': 'x'}).json()
        self.assertIn('error', response)
        self.assertFalse(Test.objects.get(id=self.other.id).finished)
        self.assertFalse(LogEvent.objects.exists())

    def test_submit_error_for_an_unassigned_test_is_refused(self):
        forged = {**self.thief, 'test_id': self.other.id, 'error': 'x', 'logs': 'y'}
        self.assertIn('error', self.client.post('/clientSubmitError/', forged).json())
        self.assertFalse(LogEvent.objects.exists())

    def test_pgn_for_a_foreign_result_is_refused(self):
        forged = {
            **self.thief,
            'result_id': self.owner['result_id'],
            'book_index': 0,
            'file': SimpleUploadedFile('games.pgn', b'x'),
        }
        self.assertIn('error', self.client.post('/clientSubmitPGN/', forged).json())
        self.assertFalse(PGN.objects.exists())

    def test_missing_secret_is_rejected_cleanly(self):
        response = self.client.post(
            '/clientHeartbeat/', {'machine_id': self.owner['machine_id'], 'test_id': self.test.id}
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn('Invalid Secret Token', response.json()['error'])

    def test_wrong_secret_is_rejected(self):
        forged = {**self.owner, 'secret': self.thief['secret']}
        response = self.client.post('/clientHeartbeat/', forged).json()
        self.assertIn('Invalid Secret Token', response['error'])


class ThrottleTests(TestCase):
    def setUp(self):
        clear_throttle(self)
        self.user = create_user('lab-worker')

    def fail_logins(self, count, username='lab-worker', **extra):
        for _ in range(count):
            self.client.post('/login/', {'username': username, 'password': 'wrong'}, **extra)

    def login(self, **extra):
        return self.client.post('/login/', credentials(self.user), **extra)

    def test_valid_login_survives_fewer_failures_than_the_limit(self):
        self.fail_logins(throttle.ACCOUNT_LIMIT - 1)
        self.assertRedirects(self.login(), '/index/', fetch_redirect_response=False)

    def test_account_is_locked_on_the_failing_address(self):
        self.fail_logins(throttle.ACCOUNT_LIMIT, REMOTE_ADDR='203.0.113.1')

        response = self.login(REMOTE_ADDR='203.0.113.1')
        self.assertRedirects(response, '/login/', fetch_redirect_response=False)
        self.assertEqual(self.client.session['error_message'], 'Too many failed logins. Try again later')

    def test_failures_elsewhere_never_lock_out_a_correct_login(self):
        self.fail_logins(throttle.ACCOUNT_LIMIT * 2, REMOTE_ADDR='203.0.113.66')

        self.assertRedirects(self.login(REMOTE_ADDR='203.0.113.2'), '/index/', fetch_redirect_response=False)
        response = self.client.post('/api/active/', register_payload(self.user), REMOTE_ADDR='203.0.113.2')
        self.assertEqual(response.status_code, 200)

    def test_address_is_locked_after_its_own_limit(self):
        for index in range(throttle.ADDRESS_LIMIT):
            self.fail_logins(1, username=f'guess{index}')
        self.assertRedirects(self.login(), '/login/', fetch_redirect_response=False)
        self.assertRedirects(self.login(REMOTE_ADDR='203.0.113.2'), '/index/', fetch_redirect_response=False)

    def test_refused_attempts_do_not_count_against_the_address(self):
        self.fail_logins(throttle.ADDRESS_LIMIT)
        other = create_user('lab-readonly')
        response = self.client.post('/login/', credentials(other))
        self.assertRedirects(response, '/index/', fetch_redirect_response=False)

    def test_worker_registration_gets_a_distinct_error(self):
        payload = register_payload(self.user)
        for _ in range(throttle.ACCOUNT_LIMIT):
            self.client.post('/clientWorkerInfo/', {**payload, 'password': 'wrong'})

        response = self.client.post('/clientWorkerInfo/', payload).json()
        self.assertEqual(response, {'error': 'Too many failed logins. Try again later'})
        self.assertFalse(Machine.objects.exists())

    def test_api_answers_429(self):
        for _ in range(throttle.ACCOUNT_LIMIT):
            self.client.post('/api/config/', {'username': 'lab-worker', 'password': 'wrong'})

        for url in ['/api/config/', '/api/active/']:
            response = self.client.post(url, register_payload(self.user))
            self.assertEqual(response.status_code, 429, url)
            self.assertEqual(response.json(), {'error': 'Too many failed logins'}, url)

    def test_admin_login_is_throttled(self):
        self.user.is_staff = True
        self.user.save()
        self.fail_logins(throttle.ACCOUNT_LIMIT)

        response = self.client.post('/admin/login/', {**credentials(self.user), 'next': '/admin/'})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.client.session.get('_auth_user_id'))

    def test_admin_failures_are_counted(self):
        for _ in range(throttle.ACCOUNT_LIMIT):
            self.client.post('/admin/login/', {'username': 'lab-worker', 'password': 'wrong', 'next': '/admin/'})
        self.assertRedirects(self.login(), '/login/', fetch_redirect_response=False)

    def test_sessions_from_the_stock_backend_stay_valid(self):
        self.client.force_login(self.user, backend='django.contrib.auth.backends.ModelBackend')
        self.assertEqual(self.client.get('/index/').status_code, 200)

    def test_path_is_logged_escaped(self):
        with self.assertLogs('OpenBench.views', 'WARNING') as logs:
            self.client.post('/api/config/x%0Aforged/', {'username': 'x', 'password': 'y'})
        self.assertEqual(len(logs.output), 1)
        self.assertNotIn('\n', logs.output[0])
        self.assertIn('\\nforged', logs.output[0])


class ClientAddressTests(TestCase):
    def request(self, forwarded=None):
        headers = {'HTTP_X_FORWARDED_FOR': forwarded} if forwarded is not None else {}
        return RequestFactory().get('/', REMOTE_ADDR='10.0.2.100', **headers)

    @override_settings(OPENBENCH_BEHIND_TLS_PROXY=False)
    def test_forwarded_header_is_ignored_without_a_proxy(self):
        self.assertEqual(throttle.client_ip(self.request('198.51.100.7')), '10.0.2.100')

    @override_settings(OPENBENCH_BEHIND_TLS_PROXY=True)
    def test_rightmost_forwarded_hop_is_used_behind_the_proxy(self):
        self.assertEqual(throttle.client_ip(self.request('1.1.1.1, 198.51.100.7')), '198.51.100.7')

    @override_settings(OPENBENCH_BEHIND_TLS_PROXY=True)
    def test_missing_forwarded_header_falls_back_to_the_peer(self):
        self.assertEqual(throttle.client_ip(self.request()), '10.0.2.100')
        self.assertEqual(throttle.client_ip(self.request('')), '10.0.2.100')


class GeneralConfigTests(TestCase):
    def test_shipped_config_is_valid(self):
        verify_general_config(copy.deepcopy(openbench_config()))

    def test_wrong_types_are_rejected(self):
        for key, value in [('client_version', '50'), ('client_repo_url', 1), ('require_login_to_view', 'true')]:
            with self.subTest(key=key), self.assertRaises(AssertionError):
                verify_general_config({**openbench_config(), key: value})

    def test_missing_keys_are_rejected(self):
        config = {**openbench_config()}
        del config['require_manual_registration']
        with self.assertRaises(AssertionError):
            verify_general_config(config)


class ViewHelperTests(TestCase):
    def test_render_keeps_the_warning_text(self):
        request = RequestFactory().get('/login/')
        SessionMiddleware(lambda request: HttpResponse()).process_request(request)
        request.user = mock.Mock(is_authenticated=False)

        content = OpenBench.views.render(request, 'login.html', always_allow=True, warning='Heads up').content.decode()
        self.assertIn('Heads up', content)

    def test_profile_config_without_profile_redirects_to_the_index(self):
        self.client.force_login(OpenBench.views.User.objects.create_user('orphan', '', PASSWORD))
        self.assertRedirects(self.client.get('/profileConfig/'), '/index/', fetch_redirect_response=False)


class CrossSiteActionTests(TestCase):
    def setUp(self):
        create_engine_config()
        ensure_book()
        self.approver = create_user('admin', approver=True)
        self.test = create_test(self.approver)
        self.client.force_login(self.approver)

    def test_cross_site_workload_action_is_refused(self):
        for site in ('cross-site', 'same-site'):
            self.client.post(f'/test/{self.test.id}/DELETE/', headers={'sec-fetch-site': site})
            self.assertFalse(Test.objects.get(id=self.test.id).deleted, site)

    def test_same_origin_workload_action_is_allowed(self):
        self.client.post(f'/test/{self.test.id}/DELETE/', headers={'sec-fetch-site': 'same-origin'})
        self.assertTrue(Test.objects.get(id=self.test.id).deleted)

    def test_cross_site_network_delete_is_refused(self):
        Network.objects.create(sha256='ABCDEF01', name='r1', engine='Avalanche', author='admin')
        self.client.post('/networks/Avalanche/DELETE/r1/', headers={'sec-fetch-site': 'cross-site'})
        self.assertTrue(Network.objects.filter(name='r1').exists())


class SecurityHeaderTests(TestCase):
    def test_login_page_headers(self):
        response = self.client.get('/login/')
        self.assertEqual(response['X-Frame-Options'], 'DENY')
        self.assertEqual(response['Referrer-Policy'], 'same-origin')
        self.assertEqual(response['Cross-Origin-Opener-Policy'], 'same-origin')
        self.assertEqual(response['X-Content-Type-Options'], 'nosniff')
        self.assertNotIn('Strict-Transport-Security', response)

    def test_cookies_are_http_only(self):
        clear_throttle(self)
        self.client.post('/login/', {'username': 'nobody', 'password': 'x'})
        self.assertTrue(self.client.cookies['sessionid']['httponly'])
        self.client.get('/login/')
        self.assertTrue(self.client.cookies['csrftoken']['httponly'])


class ApiNetworkDownloadTests(TestCase):
    def setUp(self):
        clear_throttle(self)
        create_engine_config()
        create_user('reader')

    def test_bad_password_counts_as_one_failure(self):
        with self.assertLogs('OpenBench.views', 'WARNING') as logs:
            self.client.post('/api/networks/Avalanche/r1/', {'username': 'reader', 'password': 'wrong'})
        self.assertEqual(len(logs.output), 1)
