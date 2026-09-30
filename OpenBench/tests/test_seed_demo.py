import io

from datetime import timedelta

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone

from OpenBench.models import Engine, Machine, Profile, Result, Test, WorkloadSnapshot
from OpenBench.management.commands.seed_demo import TUNES, WORKLOADS

class SeedDemoTests(TestCase):

    def test_refuses_without_debug(self):
        with self.assertRaises(CommandError):
            call_command('seed_demo')
        self.assertFalse(Test.objects.exists())

    @override_settings(DEBUG=True)
    def test_fills_an_empty_database_consistently(self):
        call_command('seed_demo', stdout=io.StringIO())

        self.assertEqual(Test.objects.count(), len(WORKLOADS) + len(TUNES))
        self.assertTrue(Machine.objects.exists())

        for engine in Engine.objects.all():
            self.assertEqual(engine.source, 'https://api.github.com/repos/SnowballSH/Avalanche/zipball/%s' % (engine.sha))

        for test in Test.objects.all():
            results = Result.objects.filter(test=test)
            self.assertEqual(test.games, sum(result.games for result in results))
            self.assertEqual(test.games, test.wins + test.losses + test.draws)
            self.assertEqual(test.games, 2 * sum(test.as_penta()))

        self.assertEqual(sum(Profile.objects.values_list('games', flat=True)), sum(Result.objects.values_list('games', flat=True)))
        self.assertEqual(sum(Profile.objects.values_list('tests', flat=True)), Test.objects.count())

    @override_settings(DEBUG=True)
    def test_every_mode_is_seeded_active_and_finished(self):
        call_command('seed_demo', stdout=io.StringIO())

        for mode in ('SPRT', 'GAMES', 'SPSA', 'DATAGEN'):
            workloads = Test.objects.filter(test_mode=mode, approved=True, games__gt=0)
            self.assertTrue(workloads.filter(finished=False).exists(), mode)
            self.assertTrue(workloads.filter(finished=True).exists(), mode)

    @override_settings(DEBUG=True)
    def test_tunes_match_their_spsa_runs(self):
        call_command('seed_demo', stdout=io.StringIO())

        for tune in Test.objects.filter(test_mode='SPSA').select_related('spsa_run'):
            run, target = tune.spsa_run, 2 * tune.spsa_run.pairs_per * tune.spsa_run.iterations
            self.assertEqual((tune.dev_id, tune.workload_size), (tune.base_id, run.pairs_per))
            self.assertEqual(tune.games % (2 * run.pairs_per), 0)
            self.assertEqual(tune.finished, tune.games == target)
            self.assertLessEqual(tune.games, target)
            self.assertFalse(tune.passed or tune.failed)

            parameters = list(run.parameters.order_by('index'))
            self.assertEqual([param.index for param in parameters], list(range(len(parameters))))
            self.assertEqual({param.is_float for param in parameters}, {True, False})
            for param in parameters:
                self.assertNotEqual(param.value, param.start)
                self.assertTrue(param.min_value <= param.value <= param.max_value)
                self.assertAlmostEqual(param.c_value, param.c_end * run.iterations ** run.gamma)
                self.assertAlmostEqual(param.a_value, param.r_end * param.c_end ** 2 * (run.a_ratio * run.iterations + run.iterations) ** run.alpha)

    @override_settings(DEBUG=True)
    def test_fixed_length_workloads_finish_at_their_target(self):
        call_command('seed_demo', stdout=io.StringIO())

        for workload in Test.objects.filter(test_mode__in=('GAMES', 'DATAGEN'), approved=True):
            self.assertEqual(workload.finished, workload.games >= workload.max_games)
            self.assertEqual((workload.elolower, workload.eloupper, workload.currentllr), (0.0, 0.0, 0.0))

        for datagen in Test.objects.filter(test_mode='DATAGEN'):
            self.assertEqual(datagen.passed, datagen.finished)
            self.assertEqual((datagen.use_tri, datagen.use_penta), (not datagen.play_reverses, datagen.play_reverses))

    @override_settings(DEBUG=True)
    def test_verdicts_follow_the_counters(self):
        call_command('seed_demo', stdout=io.StringIO())

        for test in Test.objects.filter(test_mode='SPRT', approved=True):
            self.assertEqual((test.passed, test.failed), (test.currentllr > test.upperllr, test.currentllr < test.lowerllr))
            self.assertEqual(test.finished, test.passed or test.failed)

        for test in Test.objects.filter(test_mode='GAMES', finished=True):
            self.assertEqual((test.passed, test.failed), (test.wins >= test.losses, test.wins < test.losses))

        self.assertTrue(Test.objects.filter(test_mode='SPRT', passed=True).exists())
        self.assertTrue(Test.objects.filter(test_mode='SPRT', failed=True).exists())

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
