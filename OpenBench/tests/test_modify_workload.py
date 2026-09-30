from unittest import mock

from django.test import TestCase

from OpenBench.config import OPENBENCH_CONFIG
from OpenBench.models import LogEvent
from OpenBench.tests.fixtures import PASSWORD, create_engine_config, create_test, create_user, ensure_book

class ModifyWorkloadTests(TestCase):

    def setUp(self):
        create_engine_config()
        ensure_book()
        self.author   = create_user('author')
        self.approver = create_user('approver', approver=True)
        self.other    = create_user('other')
        self.test     = create_test(self.author, approved=False)

    def act(self, user, action, data=None, workload=None):
        self.client.logout()
        if user:
            self.client.login(username=user.username, password=PASSWORD)
        workload = workload or self.test
        response = self.client.post('/test/%d/%s/' % (workload.id, action), data or {})
        self.assertEqual(response.status_code, 302)
        workload.refresh_from_db()
        session = self.client.session
        return session.get('error_message'), session.get('status_message')

    def test_anonymous_users_are_sent_to_login(self):
        error, _ = self.act(None, 'STOP')
        self.assertIn('Only users', error)
        self.assertFalse(self.test.finished)

    def test_unknown_actions_are_refused(self):
        error, _ = self.act(self.approver, 'EXPLODE')
        self.assertIn('Unknown Workload action', error)

    def test_other_users_cannot_touch_the_workload(self):
        for action in ['STOP', 'DELETE', 'MODIFY', 'APPROVE']:
            error, _ = self.act(self.other, action, { 'priority' : '9' })
            self.assertIn('another user', error)
        self.assertEqual((self.test.finished, self.test.deleted, self.test.priority, self.test.approved), (False, False, 0, False))
        self.assertFalse(LogEvent.objects.exists())

    def test_authors_cannot_approve_their_own_workload(self):
        error, _ = self.act(self.author, 'APPROVE')
        self.assertIn('cannot approve', error)
        self.assertFalse(self.test.approved)

    def test_authors_manage_their_own_workload(self):
        self.act(self.author, 'STOP')
        self.assertTrue(self.test.finished)
        self.act(self.author, 'RESTART')
        self.assertFalse(self.test.finished)
        self.act(self.author, 'DELETE')
        self.assertTrue(self.test.deleted)
        self.act(self.author, 'RESTORE')
        self.assertFalse(self.test.deleted)
        self.assertEqual(LogEvent.objects.filter(test_id=self.test.id).count(), 4)

    def test_approvers_approve_other_users_workloads(self):
        _, status = self.act(self.approver, 'APPROVE')
        self.assertEqual(status, 'Workload was Approved!')
        self.assertTrue(self.test.approved)

    def test_cross_approval_forbids_approving_your_own_workload(self):
        own = create_test(self.approver, approved=False)
        with mock.patch.dict(OPENBENCH_CONFIG, { 'use_cross_approval' : True }):
            error, _ = self.act(self.approver, 'APPROVE', workload=own)
        self.assertIn('cannot approve', error)
        self.assertFalse(own.approved)

    def test_without_cross_approval_approvers_may_approve_their_own(self):
        own = create_test(self.approver, approved=False)
        with mock.patch.dict(OPENBENCH_CONFIG, { 'use_cross_approval' : False }):
            self.act(self.approver, 'APPROVE', workload=own)
        self.assertTrue(own.approved)

    def test_superusers_may_approve_their_own_under_cross_approval(self):
        self.approver.is_superuser = True
        self.approver.save()
        own = create_test(self.approver, approved=False)
        with mock.patch.dict(OPENBENCH_CONFIG, { 'use_cross_approval' : True }):
            self.act(self.approver, 'APPROVE', workload=own)
        self.assertTrue(own.approved)

class TweakWorkloadTests(TestCase):

    def setUp(self):
        create_engine_config()
        ensure_book()
        self.author = create_user('author')
        self.test   = create_test(self.author, workload_size=32)
        self.client.login(username='author', password=PASSWORD)

    def tweak(self, **fields):
        response = self.client.post('/test/%d/MODIFY/' % (self.test.id), fields)
        self.assertEqual(response.status_code, 302)
        self.test.refresh_from_db()

    def test_valid_values_are_applied(self):
        self.tweak(priority='-3', throughput='250', workload_size='64', info='notes')
        self.assertEqual((self.test.priority, self.test.throughput, self.test.workload_size, self.test.info), (-3, 250, 64, 'notes'))

    def test_small_values_are_clamped_to_one(self):
        self.tweak(throughput='0', workload_size='-5')
        self.assertEqual((self.test.throughput, self.test.workload_size), (1, 1))

    def test_malformed_values_are_ignored(self):
        self.tweak(priority='high', throughput='2.5', workload_size='')
        self.assertEqual((self.test.priority, self.test.throughput, self.test.workload_size), (0, 1000, 32))

    def test_missing_fields_are_left_alone(self):
        self.tweak()
        self.assertEqual((self.test.priority, self.test.throughput, self.test.workload_size), (0, 1000, 32))

    def test_tunes_keep_their_workload_size(self):
        self.test.test_mode = 'SPSA'
        self.test.save()
        self.client.post('/tune/%d/MODIFY/' % (self.test.id), { 'workload_size' : '64' })
        self.test.refresh_from_db()
        self.assertEqual(self.test.workload_size, 32)
