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
