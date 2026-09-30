import copy
import json
import tempfile

from unittest import mock

from django.contrib.sessions.middleware import SessionMiddleware
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory, TestCase, override_settings

import OpenBench.views

from OpenBench.config import OPENBENCH_CONFIG, verify_general_config
from OpenBench.models import LogEvent, Machine, Network, PGN, Result, Test
from OpenBench.security import throttle
from OpenBench.tests.fixtures import (
    PASSWORD, create_engine_config, create_test, create_user, credentials, ensure_book, register_payload,
)

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
        test = create_test(self.user, dev_options='Threads=1 Hash=16 %s' % (payload))

        content = self.client.get('/test/%d/' % (test.id)).content.decode()

        self.assertNotIn(payload, content)
        self.assertIn('&lt;img/src=x/onerror=alert(1)&gt;', content)
        self.assertNotIn('innerHTML', content)
        self.assertIn('createTextNode(option)', content)

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
        return self.client.post('/scripts/', {
            **auth, 'action' : 'UPLOAD_NETWORK', 'engine' : 'Avalanche', 'name' : 'r1',
            'netfile' : SimpleUploadedFile('net.nnue', b'weights'),
        })

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

class ApiAuthenticationLoggingTests(TestCase):

    def setUp(self):
        clear_throttle(self)
        create_engine_config()
        self.user = create_user('reader')

    def test_failure_logs_one_line_without_the_password(self):
        with self.assertLogs('OpenBench.views', 'WARNING') as logs:
            response = self.client.post('/api/config/', { 'username' : 'reader', 'password' : 'hunter2-guess' })

        self.assertIn('error', json.loads(response.content))
        self.assertEqual(len(logs.output), 1)
        self.assertIn("'reader'", logs.output[0])
        self.assertNotIn('hunter2-guess', logs.output[0])
        self.assertNotIn('Traceback', logs.output[0])

    def test_valid_credentials_succeed_without_logging(self):
        with self.assertNoLogs('OpenBench.views', 'WARNING'):
            response = self.client.post('/api/config/', credentials(self.user))
        self.assertIn('engines', json.loads(response.content))

class ThrottleTests(TestCase):

    def setUp(self):
        clear_throttle(self)
        self.user = create_user('lab-worker')

    def fail_logins(self, count, username='lab-worker', **extra):
        for _ in range(count):
            self.client.post('/login/', { 'username' : username, 'password' : 'wrong' }, **extra)

    def login(self, **extra):
        return self.client.post('/login/', credentials(self.user), **extra)

    def test_valid_login_survives_fewer_failures_than_the_limit(self):
        self.fail_logins(throttle.FAILURE_LIMIT - 1)
        self.assertRedirects(self.login(), '/index/', fetch_redirect_response=False)

    def test_username_is_locked_after_the_limit(self):
        self.fail_logins(throttle.FAILURE_LIMIT, REMOTE_ADDR='203.0.113.1')

        response = self.login(REMOTE_ADDR='203.0.113.2')
        self.assertRedirects(response, '/login/', fetch_redirect_response=False)
        self.assertEqual(self.client.session['error_message'], 'Too many failed logins. Try again later')

    def test_address_is_locked_after_the_limit(self):
        for index in range(throttle.FAILURE_LIMIT):
            self.fail_logins(1, username='guess%d' % (index))
        self.assertRedirects(self.login(), '/login/', fetch_redirect_response=False)

    def test_other_addresses_and_users_are_unaffected(self):
        self.fail_logins(throttle.FAILURE_LIMIT, username='someone-else', REMOTE_ADDR='203.0.113.1')
        self.assertRedirects(self.login(REMOTE_ADDR='203.0.113.2'), '/index/', fetch_redirect_response=False)

    def test_worker_registration_is_throttled(self):
        payload = register_payload(self.user)
        for _ in range(throttle.FAILURE_LIMIT):
            self.client.post('/clientWorkerInfo/', { **payload, 'password' : 'wrong' })

        self.assertIn('error', self.client.post('/clientWorkerInfo/', payload).json())
        self.assertFalse(Machine.objects.exists())

    def test_api_is_throttled(self):
        for _ in range(throttle.FAILURE_LIMIT):
            self.client.post('/api/config/', { 'username' : 'lab-worker', 'password' : 'wrong' })
        self.assertIn('error', json.loads(self.client.post('/api/config/', credentials(self.user)).content))

    def test_admin_login_is_throttled(self):
        self.user.is_staff = True
        self.user.save()
        self.fail_logins(throttle.FAILURE_LIMIT)

        response = self.client.post('/admin/login/', { **credentials(self.user), 'next' : '/admin/' })
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.client.session.get('_auth_user_id'))

class ClientAddressTests(TestCase):

    def request(self, forwarded=None):
        headers = { 'HTTP_X_FORWARDED_FOR' : forwarded } if forwarded is not None else {}
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

class ApiNetworkDownloadTests(TestCase):

    def setUp(self):
        clear_throttle(self)
        create_engine_config()
        create_user('reader')

    def test_bad_password_counts_as_one_failure(self):
        with self.assertLogs('OpenBench.views', 'WARNING') as logs:
            self.client.post('/api/networks/Avalanche/r1/', { 'username' : 'reader', 'password' : 'wrong' })
        self.assertEqual(len(logs.output), 1)
