import hashlib
import os
import tempfile

from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import DatabaseError
from django.test import TestCase, override_settings

from OpenBench.models import Network
from OpenBench.tests.fixtures import create_engine_config, create_user

class HealthTests(TestCase):

    def test_ready_without_login(self):
        response = self.client.get('/health/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), { 'status' : 'ok' })

    def test_unavailable_when_the_database_fails(self):
        with mock.patch('OpenBench.views.connection.cursor', side_effect=DatabaseError):
            self.assertEqual(self.client.get('/health/').status_code, 503)

class LargeUploadTests(TestCase):

    def setUp(self):
        self.media = tempfile.TemporaryDirectory()
        self.addCleanup(self.media.cleanup)
        self.enterContext(override_settings(MEDIA_ROOT=self.media.name, FILE_UPLOAD_MAX_MEMORY_SIZE=1024))
        create_engine_config()
        self.client.force_login(create_user('approver', approver=True))

    def test_spooled_upload_is_hashed_and_stored_intact(self):
        content = os.urandom(3 * 1024 * 1024)
        netfile = SimpleUploadedFile('net.nnue', content)
        self.client.post('/networks/Avalanche/upload/big/', { 'netfile' : netfile })

        network = Network.objects.get(name='big')
        self.assertEqual(network.sha256, hashlib.sha256(content).hexdigest()[:8].upper())
        with open(os.path.join(self.media.name, network.sha256), 'rb') as fin:
            self.assertEqual(fin.read(), content)
