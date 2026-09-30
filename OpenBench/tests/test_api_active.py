import json
from typing import Any

from django.test import TestCase

from OpenBench.models import Machine, Result
from OpenBench.tests.fixtures import (
    create_engine_config,
    create_test,
    create_user,
    credentials,
    ensure_book,
    register_payload,
    system_info,
)


class ApiActiveTests(TestCase):
    def setUp(self):
        create_engine_config()
        ensure_book()
        self.worker = create_user('lab-worker')
        self.admin = create_user('admin', approver=True)

    def active(self, blacklist=(), user=None, **info):
        payload = {
            **credentials(user or self.worker),
            'system_info': json.dumps(system_info(**info)),
            'blacklist': list(blacklist),
        }
        return self.client.post('/api/active/', payload)

    def assignable(self, **kwargs):
        response = self.active(**kwargs)
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()['assignable']

    def assigned(self, blacklist=(), **info):
        session = self.client.post('/clientWorkerInfo/', register_payload(self.worker, **info)).json()
        payload = {'machine_id': session['machine_id'], 'secret': session['secret'], 'blacklist': list(blacklist)}
        return self.client.post('/clientGetWorkload/', payload).json().get('workload', {}).get('test', {}).get('id')

    def test_matches_assignment_for_one_and_four_thread_tests(self):
        one = create_test(self.admin, threads=1)
        four = create_test(self.admin, threads=4)

        self.assertEqual(self.assignable(concurrency=2, physical_cores=2), 1)
        self.assertEqual(self.assigned(concurrency=2, physical_cores=2), one.id)

        self.assertEqual(self.assignable(concurrency=4, physical_cores=4), 2)
        self.assertIn(self.assigned(concurrency=4, physical_cores=4), (one.id, four.id))

        self.assertEqual(self.assignable(concurrency=4, physical_cores=4, blacklist=[one.id]), 1)
        self.assertEqual(self.assigned(concurrency=4, physical_cores=4, blacklist=[one.id]), four.id)

    def test_agrees_with_assignment_across_machines(self):
        create_test(self.admin, threads=4)
        machines: list[dict[str, Any]] = [
            {'concurrency': 1, 'physical_cores': 1},
            {'concurrency': 4, 'physical_cores': 4},
            {'concurrency': 4, 'physical_cores': 4, 'cpu_flags': ()},
            {'concurrency': 4, 'physical_cores': 4, 'os_name': 'Windows'},
            {'concurrency': 4, 'physical_cores': 4, 'engines': ()},
        ]
        for info in machines:
            with self.subTest(info=info):
                self.assertEqual(self.assignable(**info) > 0, self.assigned(**info) is not None)

    def test_priority_refines_like_assignment(self):
        create_test(self.admin, threads=1, priority=0)
        high = create_test(self.admin, threads=1, priority=5)
        self.assertEqual(self.assignable(), 1)
        self.assertEqual(self.assigned(), high.id)

    def test_nothing_active(self):
        create_test(self.admin, finished=True)
        create_test(self.admin, approved=False)
        self.assertEqual(self.assignable(), 0)

    def test_writes_nothing(self):
        create_test(self.admin)
        self.assignable()
        self.assertFalse(Machine.objects.exists())
        self.assertFalse(Result.objects.exists())

    def test_rejects_bad_credentials_and_disabled_users(self):
        disabled = create_user('disabled', enabled=False)
        self.assertEqual(self.active(user=disabled).status_code, 401)

        payload = {'username': 'lab-worker', 'password': 'wrong', 'system_info': json.dumps(system_info())}
        self.assertEqual(self.client.post('/api/active/', payload).status_code, 401)

    def test_rejects_malformed_input(self):
        self.assertEqual(self.active(concurrency='4').status_code, 400)
        self.assertEqual(self.active(noisy=0).status_code, 400)
        self.assertEqual(self.active(blacklist=['1; DROP']).status_code, 400)
        self.assertEqual(self.active(focus='Avalanche').status_code, 400)

        missing = {**credentials(self.worker), 'system_info': json.dumps({'concurrency': 4})}
        self.assertEqual(self.client.post('/api/active/', missing).status_code, 400)
        self.assertEqual(self.client.post('/api/active/', credentials(self.worker)).status_code, 400)

    def test_requires_post(self):
        self.assertEqual(self.client.get('/api/active/').status_code, 405)
