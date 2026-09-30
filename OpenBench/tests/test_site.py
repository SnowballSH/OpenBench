import tempfile
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from OpenBench.models import Network
from OpenBench.tests.fixtures import PASSWORD, create_engine_config, create_user


class PageTests(TestCase):
    def setUp(self):
        self.user = create_user('reader')

    def test_anonymous_views_redirect_to_login(self):
        for url in ['/index/', '/machines/', '/networks/', '/users/']:
            response = self.client.get(url)
            self.assertRedirects(response, '/login/', fetch_redirect_response=False, msg_prefix=url)

    def test_registration_is_manual(self):
        self.assertRedirects(self.client.get('/register/'), '/login/', fetch_redirect_response=False)

    def test_login_renders_the_sidebar(self):
        response = self.client.get('/login/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '<div id="sidebar">')

    def test_login_views_logout(self):
        response = self.client.post('/login/', {'username': 'reader', 'password': PASSWORD})
        self.assertRedirects(response, '/index/', fetch_redirect_response=False)

        for url in ['/index/', '/machines/', '/networks/', '/users/', '/manage/books/']:
            self.assertEqual(self.client.get(url).status_code, 200, url)

        self.client.post('/logout/')
        self.assertRedirects(self.client.get('/index/'), '/login/', fetch_redirect_response=False)

    def test_bad_password_is_rejected(self):
        response = self.client.post('/login/', {'username': 'reader', 'password': 'wrong'})
        self.assertRedirects(response, '/login/', fetch_redirect_response=False)


class NetworkTests(TestCase):
    def setUp(self):
        self.media = tempfile.TemporaryDirectory()
        self.enterContext(override_settings(MEDIA_ROOT=self.media.name))
        self.enterContext(mock.patch('OpenBench.utils.MEDIA_ROOT', self.media.name))
        self.addCleanup(self.media.cleanup)
        create_engine_config()
        self.approver = create_user('approver', approver=True)
        self.client.force_login(self.approver)

    def upload(self, name, content):
        netfile = SimpleUploadedFile('net.nnue', content)
        return self.client.post(f'/networks/Avalanche/upload/{name}/', {'netfile': netfile})

    def test_upload_then_download(self):
        content = b'nnue-weights' * 1000
        self.assertRedirects(self.upload('r1-ctrl', content), '/networks/Avalanche/', fetch_redirect_response=False)

        network = Network.objects.get(name='r1-ctrl')
        response = self.client.get('/networks/Avalanche/download/r1-ctrl/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.getvalue(), content)
        self.assertEqual(response['Content-Disposition'], f'attachment; filename={network.sha256}')

    def test_duplicate_upload_is_rejected(self):
        self.upload('r1-ctrl', b'same')
        self.upload('r1-copy', b'same')
        self.assertEqual(Network.objects.count(), 1)

    def test_non_approver_cannot_upload(self):
        self.client.force_login(create_user('plain'))
        self.assertRedirects(self.upload('r1-ctrl', b'x'), '/index/', fetch_redirect_response=False)
        self.assertFalse(Network.objects.exists())
