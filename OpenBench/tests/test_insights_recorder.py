from datetime import UTC, datetime, timedelta
from itertools import pairwise
from unittest import mock

from django.db import connection
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext

from OpenBench.insights.recorder import (
    RECENT_KEPT,
    SNAPSHOT_INTERVAL,
    SNAPSHOT_LIMIT,
    SNAPSHOT_TARGET,
    record_snapshot,
    should_record,
    thinned_ids,
)
from OpenBench.models import Result, Test, WorkloadSnapshot
from OpenBench.tests.fixtures import (
    create_engine_config,
    create_test,
    create_user,
    ensure_book,
    present,
    register_payload,
)

T0 = datetime(2026, 9, 1, tzinfo=UTC)


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


class ThrottleTests(SimpleTestCase):
    def test_should_record(self):
        self.assertTrue(should_record(None, at(0), finished=False))
        self.assertFalse(should_record(at(0), at(59), finished=False))
        self.assertTrue(should_record(at(0), at(60), finished=False))
        self.assertTrue(should_record(at(0), at(1), finished=True))


class ThinningTests(SimpleTestCase):
    def test_small_histories_are_kept(self):
        points = [(i, at(60 * i)) for i in range(SNAPSHOT_TARGET + 1)]
        self.assertEqual(thinned_ids(points), [])

    def test_thinning_is_uniform_in_time_and_keeps_the_ends(self):
        points = [(i, at(60 * i)) for i in range(SNAPSHOT_LIMIT + 1)]
        doomed = set(thinned_ids(points))
        kept = [i for i, _ in points if i not in doomed]

        recent = [i for i, created in points if created >= points[-1][1] - RECENT_KEPT]

        self.assertIn(0, kept)
        self.assertTrue(set(recent) <= set(kept))
        self.assertLessEqual(len(kept), SNAPSHOT_TARGET + 1 + len(recent))
        self.assertGreaterEqual(len(kept), SNAPSHOT_TARGET + len(recent))
        self.assertLessEqual(max(b - a for a, b in pairwise(kept)), 3)

    def test_the_last_hour_is_never_thinned(self):
        points = [(i, at(i)) for i in range(SNAPSHOT_LIMIT + 1)]
        self.assertEqual(thinned_ids(points), [])

    def test_identical_timestamps(self):
        points = [(i, at(0)) for i in range(SNAPSHOT_LIMIT)] + [(SNAPSHOT_LIMIT, at(7200))]
        self.assertEqual(len(points) - len(thinned_ids(points)), 3)


class RecorderTests(TestCase):
    def setUp(self):
        ensure_book()
        self.test = create_test(
            create_user('admin', approver=True), games=10, LL=1, DD=3, WW=1, losses=3, draws=4, wins=3, currentllr=0.25
        )

    def test_records_first_then_throttles(self):
        self.assertIsNotNone(record_snapshot(self.test, 10, at(0)))
        self.assertIsNone(record_snapshot(self.test, 10, at(30)))
        self.assertIsNotNone(record_snapshot(self.test, 10, at(0) + SNAPSHOT_INTERVAL))

        snapshot = present(WorkloadSnapshot.objects.order_by('created').first())
        self.assertEqual((snapshot.games, snapshot.LL, snapshot.DD, snapshot.WW, snapshot.llr), (10, 1, 3, 1, 0.25))
        self.assertEqual(WorkloadSnapshot.objects.count(), 2)

    def test_finishing_always_records(self):
        record_snapshot(self.test, 10, at(0))
        self.test.finished = True
        self.assertIsNotNone(record_snapshot(self.test, 10, at(1)))

    def test_costs_one_query_when_throttled(self):
        record_snapshot(self.test, 10, at(0))
        with CaptureQueriesContext(connection) as queries:
            record_snapshot(self.test, 10, at(1))
        self.assertEqual(len(queries), 1)

    def test_costs_one_query_and_an_insert_when_recording(self):
        record_snapshot(self.test, 10, at(0))
        with CaptureQueriesContext(connection) as queries:
            record_snapshot(self.test, 10, at(120))
        self.assertEqual(len(queries), 2)

    def test_history_is_capped(self):
        WorkloadSnapshot.objects.bulk_create(
            WorkloadSnapshot(test=self.test, created=at(60 * i)) for i in range(SNAPSHOT_LIMIT)
        )
        record_snapshot(self.test, 10, at(60 * SNAPSHOT_LIMIT))

        created = list(
            WorkloadSnapshot.objects.filter(test=self.test).order_by('created').values_list('created', flat=True)
        )
        self.assertLess(len(created), SNAPSHOT_LIMIT)
        self.assertEqual((created[0], created[-1]), (at(0), at(60 * SNAPSHOT_LIMIT)))
        self.assertEqual(sum(1 for moment in created if moment >= created[-1] - RECENT_KEPT), 61)

    def test_first_snapshot_of_a_running_workload_anchors_its_creation(self):
        self.test.creation = at(-3600)
        record_snapshot(self.test, 4, at(0))
        history = list(WorkloadSnapshot.objects.order_by('created').values_list('created', 'games'))
        self.assertEqual(history, [(at(-3600), 0), (at(0), 10)])

    def test_first_report_needs_no_anchor(self):
        record_snapshot(self.test, 10, at(0))
        self.assertEqual(WorkloadSnapshot.objects.count(), 1)

    def test_history_is_deleted_with_its_test(self):
        record_snapshot(self.test, 10, at(0))
        Test.objects.filter(id=self.test.id).delete()
        self.assertFalse(WorkloadSnapshot.objects.exists())


class UpdateTestIntegrationTests(TestCase):
    def setUp(self):
        create_engine_config()
        ensure_book()
        self.worker = create_user('lab-worker')
        self.test = create_test(create_user('admin', approver=True), test_mode='GAMES', max_games=18)

    def submit(self, session, result_id, penta='0 1 1 1 0'):
        payload = {
            **session,
            'test_id': self.test.id,
            'result_id': result_id,
            'crashes': 0,
            'timelosses': 0,
            'illegals': 0,
            'trinomial': '1 2 3',
            'pentanomial': penta,
        }
        return self.client.post('/clientSubmitResults/', payload).json()

    def session(self):
        response = self.client.post('/clientWorkerInfo/', register_payload(self.worker)).json()
        session = {'machine_id': response['machine_id'], 'secret': response['secret']}
        workload = self.client.post('/clientGetWorkload/', session).json()['workload']
        return session, workload['result']['id']

    def test_results_are_recorded_and_throttled(self):
        session, result = self.session()

        self.assertEqual(self.submit(session, result), {})
        self.assertEqual(list(WorkloadSnapshot.objects.values_list('games', flat=True)), [6])

        self.assertEqual(self.submit(session, result), {})
        self.assertEqual(WorkloadSnapshot.objects.count(), 1)

        self.assertEqual(self.submit(session, result), {'stop': True})
        self.assertEqual(list(WorkloadSnapshot.objects.order_by('created').values_list('games', flat=True)), [6, 18])
        self.assertEqual(Result.objects.get(id=result).games, 18)

    def test_a_failing_recorder_never_loses_results(self):
        session, result = self.session()
        with (
            mock.patch('OpenBench.insights.recorder.record_snapshot', side_effect=RuntimeError('boom')),
            self.assertLogs('OpenBench.insights.recorder', level='ERROR'),
        ):
            self.assertEqual(self.submit(session, result), {})
        self.assertEqual(Result.objects.get(id=result).games, 6)
        self.test.refresh_from_db()
        self.assertEqual(self.test.games, 6)
        self.assertFalse(WorkloadSnapshot.objects.exists())
