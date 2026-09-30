import io

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from OpenBench.models import Machine, Result, Test
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

    @override_settings(DEBUG=True)
    def test_refuses_a_database_with_workloads(self):
        call_command('seed_demo', stdout=io.StringIO())
        with self.assertRaises(CommandError):
            call_command('seed_demo')
