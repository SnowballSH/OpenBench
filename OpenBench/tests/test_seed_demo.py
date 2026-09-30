import io

from datetime import timedelta

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone

from OpenBench.models import Machine, Profile, Result, Test, WorkloadSnapshot
from OpenBench.management.commands.seed_demo import WORKLOADS

class SeedDemoTests(TestCase):

    def test_refuses_without_debug(self):
        with self.assertRaises(CommandError):
            call_command('seed_demo')
        self.assertFalse(Test.objects.exists())

    @override_settings(DEBUG=True)
    def test_fills_an_empty_database_consistently(self):
        call_command('seed_demo', stdout=io.StringIO())

        self.assertEqual(Test.objects.count(), len(WORKLOADS))
        self.assertTrue(Machine.objects.exists())

        for test in Test.objects.all():
            results = Result.objects.filter(test=test)
            self.assertEqual(test.games, sum(result.games for result in results))
            self.assertEqual(test.games, test.wins + test.losses + test.draws)
            self.assertEqual(test.games, 2 * sum(test.as_penta()))

        self.assertEqual(sum(Profile.objects.values_list('games', flat=True)), sum(Result.objects.values_list('games', flat=True)))

    @override_settings(DEBUG=True)
    def test_histories_end_at_the_workload_counters(self):
        call_command('seed_demo', stdout=io.StringIO())

        for test in Test.objects.all():
            history = list(WorkloadSnapshot.objects.filter(test=test).order_by('created'))
            self.assertEqual(bool(history), test.games > 0)
            if history:
                games = [snapshot.games for snapshot in history]
                self.assertEqual(games, sorted(games))
                self.assertEqual((history[-1].games, history[-1].llr), (test.games, test.currentllr))
                self.assertGreater(history[0].created, test.creation)

    @override_settings(DEBUG=True)
    def test_refuses_a_database_with_workloads(self):
        call_command('seed_demo', stdout=io.StringIO())
        with self.assertRaises(CommandError):
            call_command('seed_demo')

    @override_settings(DEBUG=True)
    def test_fleet_has_online_and_offline_machines(self):
        call_command('seed_demo', stdout=io.StringIO())

        now    = timezone.now()
        online = Machine.objects.filter(updated__gte=now - timedelta(minutes=2))
        self.assertTrue(online.exists())
        self.assertTrue(Machine.objects.filter(updated__lt=now - timedelta(hours=1), updated__gte=now - timedelta(days=1)).exists())
        self.assertTrue(Machine.objects.filter(updated__lt=now - timedelta(days=7)).exists())

        for machine in Machine.objects.exclude(id__in=online):
            self.assertFalse(Result.objects.filter(machine=machine, updated__gt=machine.updated).exists())
