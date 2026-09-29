from django.test import TestCase

from OpenBench.models import Machine, Result, Test
from OpenBench.tests.fixtures import ensure_book, create_engine_config, create_test, create_user, register_payload

class ClientSessionTests(TestCase):

    def setUp(self):
        create_engine_config()
        ensure_book()
        self.worker = create_user('lab-worker')
        self.test   = create_test(create_user('admin', approver=True))

    def register(self, **info):
        response = self.client.post('/clientWorkerInfo/', register_payload(self.worker, **info)).json()
        return { 'machine_id' : response['machine_id'], 'secret' : response['secret'] }

    def test_build_info_lists_the_engine(self):
        build = self.client.get('/clientGetBuildInfo/').json()
        self.assertEqual(build['Avalanche']['compilers'], ['zig>=0.16.0'])
        self.assertEqual(build['Avalanche']['cpuflags'], ['AVX2'])
        self.assertFalse(build['Avalanche']['private'])

    def test_disabled_user_cannot_register(self):
        disabled = create_user('disabled', enabled=False)
        response = self.client.post('/clientWorkerInfo/', register_payload(disabled)).json()
        self.assertEqual(response, { 'error' : 'Bad Credentials' })

    def test_full_session(self):
        session  = self.register()
        workload = self.client.post('/clientGetWorkload/', session).json()['workload']
        self.assertEqual(workload['test']['id'], self.test.id)

        results = {
            **session,
            'test_id' : self.test.id, 'result_id' : workload['result']['id'],
            'crashes' : 0, 'timelosses' : 0, 'illegals' : 0,
            'trinomial' : '1 2 3', 'pentanomial' : '0 1 1 1 0',
            'dev_nodes' : 0, 'dev_time' : 0, 'dev_time_scaled' : 0,
            'base_nodes' : 0, 'base_time' : 0, 'base_time_scaled' : 0,
        }
        self.assertEqual(self.client.post('/clientSubmitResults/', results).json(), {})

        self.test.refresh_from_db()
        self.assertEqual((self.test.losses, self.test.draws, self.test.wins), (1, 2, 3))
        self.assertEqual(Result.objects.get(test=self.test).games, 6)

    def test_machine_without_avx2_gets_nothing(self):
        session = self.register(cpu_flags=())
        self.assertEqual(self.client.post('/clientGetWorkload/', session).json(), {})
        self.assertEqual(Machine.objects.get(id=session['machine_id']).info['supported'], [])
