from datetime import timedelta

import OpenBench.views

from django.test import TestCase
from django.utils import timezone

from OpenBench.models import Machine, Result, Test, WorkloadSnapshot
from OpenBench.tests.fixtures import (
    PASSWORD, create_engine_config, create_test, create_user, credentials, ensure_book, register_payload, system_info)

PENTA = (5, 40, 100, 45, 10)

def machine(owner, cpu_name, isa_name, name='None', concurrency=4):
    info = { **system_info(concurrency=concurrency), 'cpu_name' : cpu_name, 'isa_name' : isa_name, 'machine_name' : name }
    return Machine.objects.create(user=owner, info=info, mnps=1.5)

def result(test, host, penta, nodes=(0, 0, 0, 0, 0, 0)):
    wins, losses = 2 * penta[4] + penta[3], 2 * penta[0] + penta[1]
    return Result.objects.create(
        test=test, machine=host, games=2 * sum(penta), wins=wins, losses=losses, draws=2 * sum(penta) - wins - losses,
        LL=penta[0], LD=penta[1], DD=penta[2], DW=penta[3], WW=penta[4],
        dev_nodes=nodes[0], dev_time=nodes[1], dev_time_scaled=nodes[2], base_nodes=nodes[3], base_time=nodes[4], base_time_scaled=nodes[5])

class InsightsApiTests(TestCase):

    def setUp(self):
        ensure_book()
        self.reader = create_user('reader')
        self.admin  = create_user('admin', approver=True)
        self.worker = create_user('lab-worker')

        wins, losses = 2 * PENTA[4] + PENTA[3], 2 * PENTA[0] + PENTA[1]
        self.test = create_test(self.admin, games=2 * sum(PENTA), wins=wins, losses=losses, draws=2 * sum(PENTA) - wins - losses,
            LL=PENTA[0], LD=PENTA[1], DD=PENTA[2], DW=PENTA[3], WW=PENTA[4], currentllr=0.8)

        self.fast = machine(self.worker, 'Ryzen 9', 'avx512', name='fast-box', concurrency=8)
        self.slow = machine(self.admin, 'Apple M4', 'apple')
        result(self.test, self.fast, (3, 30, 70, 35, 8), nodes=(1000, 10, 20, 900, 10, 20))
        result(self.test, self.slow, (2, 10, 30, 10, 2))

        now = timezone.now()
        Test.objects.filter(id=self.test.id).update(creation=now - timedelta(hours=3))
        for hours, fraction in ((2, 0.25), (1, 0.5), (0.5, 1.0)):
            penta = [round(n * fraction) for n in PENTA]
            WorkloadSnapshot.objects.create(test=self.test, created=now - timedelta(hours=hours), games=2 * sum(penta),
                LL=penta[0], LD=penta[1], DD=penta[2], DW=penta[3], WW=penta[4], llr=0.8 * fraction)

    def login(self):
        self.client.post('/login/', { 'username' : 'reader', 'password' : PASSWORD })

    def insights(self):
        return self.client.get('/api/workload/%d/insights/' % (self.test.id)).json()

    def test_anonymous_is_rejected(self):
        self.assertIn('error', self.insights())
        self.assertEqual(self.client.get('/api/insights/server/').status_code, 401)

    def test_credentials_in_post_are_accepted(self):
        response = self.client.post('/api/workload/%d/insights/' % (self.test.id), credentials(self.reader)).json()
        self.assertIn('insights', response)
        self.assertEqual(self.client.post('/api/insights/server/', credentials(self.reader)).status_code, 200)

    def test_workload_shape(self):
        self.login()
        insights = self.insights()['insights']

        self.assertEqual(set(insights), { 'generated_at', 'workload', 'progress', 'timing', 'eta', 'strength', 'history', 'contributions' })
        self.assertEqual(insights['workload']['id'], self.test.id)
        self.assertEqual(insights['workload']['status'], 'active')
        self.assertEqual(insights['progress']['pentanomial'], list(PENTA))
        self.assertEqual(insights['progress']['games'], 2 * sum(PENTA))
        self.assertEqual((insights['progress']['llr'], insights['progress']['llr_upper']), (0.8, 2.94))

        self.assertIsInstance(insights['timing']['elapsed_seconds'], float)
        self.assertIsInstance(insights['timing']['overall']['games_per_hour'], float)
        self.assertEqual(set(insights['eta']), { 'kind', 'remaining_games', 'remaining_seconds', 'completes_at' })
        self.assertEqual(set(insights['strength']), { 'elo', 'normalized_elo', 'los', 'draw_ratio', 'penta_fractions' })
        self.assertEqual(set(insights['strength']['elo']), { 'lower', 'value', 'upper' })

        points = insights['history']['points']
        self.assertFalse(insights['history']['synthetic'])
        self.assertEqual([p['games'] for p in points], sorted(p['games'] for p in points))
        self.assertEqual(set(points[0]), { 'timestamp', 'games', 'llr', 'elo', 'elo_lower', 'elo_upper' })

        machines = insights['contributions']['machines']
        self.assertEqual([m['machine_name'] for m in machines], ['fast-box', None])
        self.assertEqual(machines[0]['owner'], 'lab-worker')
        self.assertEqual(set(machines[0]['stats']), { 'games', 'pairs', 'share', 'pairs_per_hour', 'elo' })
        self.assertAlmostEqual(sum(m['stats']['share'] for m in machines), 1.0)
        self.assertEqual([c['cpu_name'] for c in insights['contributions']['cpus']], ['Ryzen 9', 'Apple M4'])

    def test_legacy_workload_without_history(self):
        WorkloadSnapshot.objects.all().delete()
        self.login()
        history = self.insights()['insights']['history']
        self.assertTrue(history['synthetic'])
        self.assertEqual([p['games'] for p in history['points']], [2 * sum(PENTA)])

    def test_unknown_query_lists_insights(self):
        self.login()
        self.assertIn('insights', self.client.get('/api/workload/%d/nothing/' % (self.test.id)).json()['error'])

    def test_server_shape(self):
        Machine.objects.filter(id=self.fast.id).update(workload=self.test.id)
        self.login()
        server = self.client.get('/api/insights/server/').json()['server']

        self.assertEqual(set(server), { 'generated_at', 'fleet', 'workloads', 'games_last_24h', 'finished_last_7d', 'top_contributors' })
        self.assertEqual((server['fleet']['machines'], server['fleet']['threads']), (2, 12))
        self.assertEqual(server['workloads'], { 'pending' : 0, 'active' : 1 })
        self.assertEqual(server['games_last_24h'], 2 * sum(PENTA))
        self.assertEqual(server['finished_last_7d']['total'], 0)
        self.assertIsNone(server['finished_last_7d']['sprt_pass_rate'])
        self.assertEqual(server['top_contributors'], [])

    def test_result_summaries_keep_their_shape(self):
        summary = OpenBench.views.fetch_result_summaries(self.test)
        self.assertEqual(list(summary), ['user', 'cpu_name', 'isa_name'])
        self.assertEqual(summary['cpu_name'], [
            { 'key' : 'Ryzen 9', 'penta' : '(3, 30, 70, 35, 8)', 'elo' : '17.86 ± 24.48', 'pairs' : 146, 'percent' : '73.00',
              'dev_nps' : 100000, 'dev_nps_scaled' : 50000, 'base_nps' : 90000, 'base_nps_scaled' : 45000 },
            { 'key' : 'Apple M4', 'penta' : '(2, 10, 30, 10, 2)', 'elo' : '0.00 ± 38.88', 'pairs' : 54, 'percent' : '27.00',
              'dev_nps' : 0, 'dev_nps_scaled' : 0, 'base_nps' : 0, 'base_nps_scaled' : 0 },
        ])
        self.assertEqual([row['key'] for row in summary['user']], ['lab-worker', 'admin'])

class RunningAtDeployTests(TestCase):

    PRIOR = { 'games' : 180_000, 'wins' : 31_500, 'losses' : 31_500, 'draws' : 117_000,
              'LL' : 4_500, 'LD' : 22_500, 'DD' : 36_000, 'DW' : 22_500, 'WW' : 4_500 }

    def setUp(self):
        create_engine_config()
        ensure_book()
        self.worker = create_user('lab-worker')
        self.test   = create_test(create_user('admin', approver=True), test_mode='GAMES', max_games=1_000_000)

        response = self.client.post('/clientWorkerInfo/', register_payload(self.worker)).json()
        self.session = { 'machine_id' : response['machine_id'], 'secret' : response['secret'] }
        self.result  = self.client.post('/clientGetWorkload/', self.session).json()['workload']['result']['id']

        Test.objects.filter(id=self.test.id).update(creation=timezone.now() - timedelta(hours=300), **self.PRIOR)
        Result.objects.filter(id=self.result).update(**self.PRIOR)

        batch = {
            **self.session, 'test_id' : self.test.id, 'result_id' : self.result,
            'crashes' : 0, 'timelosses' : 0, 'illegals' : 0,
            'trinomial' : '60 80 60', 'pentanomial' : '10 20 40 20 10',
        }
        self.assertEqual(self.client.post('/clientSubmitResults/', batch).json(), {})
        self.client.post('/login/', { 'username' : 'lab-worker', 'password' : PASSWORD })

    def test_history_starts_at_creation(self):
        insights = self.client.get('/api/workload/%d/insights/' % (self.test.id)).json()['insights']
        self.test.refresh_from_db()

        self.assertEqual([p['games'] for p in insights['history']['points']], [0, 180_200])
        self.assertEqual(insights['timing']['started_at'], self.test.creation.isoformat())
        self.assertAlmostEqual(insights['timing']['overall']['games_per_hour'], 600, delta=5)
        self.assertAlmostEqual(insights['contributions']['machines'][0]['stats']['pairs_per_hour'], 300, delta=5)

    def test_games_last_24h_excludes_earlier_play(self):
        server = self.client.get('/api/insights/server/').json()['server']
        self.assertAlmostEqual(server['games_last_24h'], 180_200 * 24 / 300, delta=50)
