import io
from datetime import timedelta

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from OpenBench.diagnosis.domain import DiagnosisState
from OpenBench.diagnosis.report import diagnose_workload
from OpenBench.fleet.pools import pool_label
from OpenBench.fleet.sessions import never_used
from OpenBench.management.commands.seed_demo import (
    BATCH_CPU,
    BATCH_IDLE_HOSTS_HOURS_AGO,
    BATCH_PLAYING_HOSTS,
    BENCH_DRIFT,
    BENCH_MISMATCH_WORKLOAD,
    BUILD_FAILURE_WORKLOAD,
    BUILD_FAILURES,
    CHAIN_ROOT,
    COMMIT_CHAIN,
    GAME_ERROR_WORKLOAD,
    LTC,
    PAST_SPRTS,
    SPEED_HOST_NOISE,
    STC,
    TUNES,
    WORKLOADS,
    DemoCommit,
    DemoStage,
    chain_workloads,
    commit_sha,
    progress_checks,
)
from OpenBench.models import Engine, LogEvent, Machine, Profile, Result, Test, WorkloadSnapshot
from OpenBench.progress.domain import TimeClass, Window
from OpenBench.progress.report import progress_report
from OpenBench.tests.fixtures import present, temporary_media
from OpenBench.triage.actions import action_rows, operator_events
from OpenBench.triage.domain import Standing
from OpenBench.triage.groups import error_events, group_rows, with_affected
from OpenBench.triage.kinds import ErrorKind
from OpenBench.triage.logs import log_path
from OpenBench.triage.query import ErrorQuery


class SeedDemoTests(TestCase):
    def setUp(self):
        temporary_media(self)

    def test_refuses_without_debug(self):
        with self.assertRaises(CommandError):
            call_command('seed_demo')
        self.assertFalse(Test.objects.exists())

    @override_settings(DEBUG=True)
    def test_fills_an_empty_database_consistently(self):
        call_command('seed_demo', stdout=io.StringIO())

        chained = sum(len(commit.stages) for commit in COMMIT_CHAIN) + len(progress_checks(COMMIT_CHAIN))
        self.assertEqual(Test.objects.count(), len(WORKLOADS) + len(PAST_SPRTS) + chained + len(TUNES))
        self.assertTrue(Machine.objects.exists())

        for engine in Engine.objects.all():
            self.assertEqual(engine.source, f'https://api.github.com/repos/SnowballSH/Avalanche/zipball/{engine.sha}')

        for test in Test.objects.all():
            results = Result.objects.filter(test=test)
            self.assertEqual(test.games, sum(result.games for result in results))
            self.assertEqual(test.games, test.wins + test.losses + test.draws)
            self.assertEqual(test.games, 2 * sum(test.as_penta()))

        self.assertEqual(
            sum(Profile.objects.values_list('games', flat=True)), sum(Result.objects.values_list('games', flat=True))
        )
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
                self.assertAlmostEqual(param.c_value, param.c_end * run.iterations**run.gamma)
                self.assertAlmostEqual(
                    param.a_value,
                    param.r_end * param.c_end**2 * (run.a_ratio * run.iterations + run.iterations) ** run.alpha,
                )

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
            self.assertEqual(
                (test.passed, test.failed), (test.currentllr > test.upperllr, test.currentllr < test.lowerllr)
            )
            self.assertTrue(test.finished or not (test.passed or test.failed))

        stopped = Test.objects.filter(test_mode='SPRT', finished=True, passed=False, failed=False)
        self.assertEqual(stopped.count(), sum(spec.state == 'stopped' for spec in (*WORKLOADS, *PAST_SPRTS)))

        for test in Test.objects.filter(test_mode='GAMES', finished=True):
            self.assertEqual((test.passed, test.failed), (test.wins >= test.losses, test.wins < test.losses))

        self.assertTrue(Test.objects.filter(test_mode='SPRT', passed=True).exists())
        self.assertTrue(Test.objects.filter(test_mode='SPRT', failed=True).exists())

    @override_settings(DEBUG=True)
    def test_past_sprts_spread_over_six_months(self):
        call_command('seed_demo', stdout=io.StringIO())

        now = timezone.now()
        past = Test.objects.filter(test_mode='SPRT', finished=True, updated__lt=now - timedelta(days=7))
        self.assertEqual(past.count(), len(PAST_SPRTS))
        self.assertLess(min(past.values_list('creation', flat=True)), now - timedelta(days=150))
        self.assertEqual(set(past.values_list('author', flat=True)), {'admin', 'lab-worker', 'home-worker'})
        for test in past:
            self.assertEqual(WorkloadSnapshot.objects.filter(test=test).latest('created').created, test.updated)

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

        now = timezone.now()
        online = Machine.objects.filter(updated__gte=now - timedelta(minutes=2))
        self.assertTrue(online.exists())
        self.assertTrue(
            Machine.objects.filter(updated__lt=now - timedelta(hours=1), updated__gte=now - timedelta(days=1)).exists()
        )
        self.assertTrue(Machine.objects.filter(updated__lt=now - timedelta(days=7)).exists())

        for machine in Machine.objects.exclude(id__in=online):
            self.assertFalse(Result.objects.filter(machine=machine, updated__gt=machine.updated).exists())

    @override_settings(DEBUG=True)
    def test_a_supervised_host_and_ephemeral_jobs_are_seeded(self):
        call_command('seed_demo', stdout=io.StringIO())

        supervised = Machine.objects.filter(info__machine_name='demo-3')
        self.assertGreater(supervised.count(), 10)
        self.assertEqual(supervised.values('host_key').distinct().count(), 1)
        self.assertTrue(never_used(supervised).exists())

        jobs = Machine.objects.filter(info__cpu_name=BATCH_CPU)
        names = set(jobs.values_list('info__machine_name', flat=True))
        failed = BUILD_FAILURES - 1
        hosts = BATCH_PLAYING_HOSTS + len(BATCH_IDLE_HOSTS_HOURS_AGO) + failed
        self.assertEqual((len(names), jobs.values('host_key').distinct().count()), (hosts, hosts))
        self.assertEqual({pool_label(name, BATCH_CPU) for name in names}, {'batch-*'})
        self.assertEqual(Result.objects.filter(machine__in=jobs).count(), BATCH_PLAYING_HOSTS)
        self.assertFalse(jobs.filter(updated__gte=timezone.now() - timedelta(minutes=2)).exists())

    @override_settings(DEBUG=True)
    def test_commit_chain_is_pinned_like_the_lab_agent_pins_it(self):
        call_command('seed_demo', stdout=io.StringIO())

        pinned = list(
            Test.objects.filter(dev__name__regex='^[0-9a-f]{40}$').select_related('dev', 'base').order_by('id')
        )
        self.assertEqual(
            [(test.dev.sha, test.dev_time_control) for test in pinned],
            [(commit_sha(commit.subject), stage.tc) for commit in COMMIT_CHAIN for stage in commit.stages],
        )

        benches: dict[str, set[int]] = {}
        for test in pinned:
            self.assertEqual((test.dev.name, test.base.name), (test.dev.sha, test.base.sha))
            self.assertEqual(test.test_mode, 'SPRT')
            subject, tag = test.info.split('\n')
            self.assertIn(subject, {commit.subject for commit in COMMIT_CHAIN})
            self.assertEqual(tag, f'avl:{test.dev.sha[:12]}')
            self.assertEqual(test.upload_pgns, 'FALSE')
            for engine in (test.dev, test.base):
                benches.setdefault(engine.sha, set()).add(engine.bench)
        self.assertEqual({len(values) for values in benches.values()}, {1})

        finished = [test.updated for test in pinned if test.finished]
        self.assertEqual(finished, sorted(finished))
        self.assertTrue(any(not test.finished for test in pinned))
        self.assertTrue(any(len(commit.subject) >= 80 for commit in COMMIT_CHAIN))

        accepted_at = {commit_sha(CHAIN_ROOT): min(test.creation for test in pinned)}
        for test in pinned:
            self.assertGreaterEqual(test.creation, accepted_at[test.base.sha])
            if test.passed and test.dev_time_control == LTC:
                accepted_at[test.dev.sha] = test.updated

        ltc = [test for test in pinned if test.dev_time_control == LTC]
        self.assertTrue(ltc)
        for test in ltc:
            self.assertEqual(test.dev_options, 'Threads=1 Hash=64')

    @override_settings(DEBUG=True)
    def test_worker_errors_and_operator_events_are_seeded(self):
        call_command('seed_demo', stdout=io.StringIO())
        now = timezone.now()

        groups = {(row.group.signature.kind, present(row.workload).dev.name): row for row in seeded_error_rows(now)}
        build = groups[ErrorKind.BUILD, BUILD_FAILURE_WORKLOAD]
        bench = groups[ErrorKind.BENCH, BENCH_MISMATCH_WORKLOAD]
        crash = groups[ErrorKind.CRASH, GAME_ERROR_WORKLOAD]

        self.assertEqual((build.group.count, build.verdict.standing), (BUILD_FAILURES, Standing.HAPPENING))
        self.assertEqual((present(build.affected).hosts, present(build.affected).pruned), (BUILD_FAILURES - 1, 1))
        self.assertEqual([pool.label for pool in present(build.affected).pools], ['batch-*'])
        self.assertEqual((present(bench.bench).difference, bench.verdict.reason), (BENCH_DRIFT, 'workload finished'))
        self.assertEqual((crash.group.count, crash.verdict.standing), (2, Standing.RESOLVED))
        self.assertIn((ErrorKind.ILLEGAL, GAME_ERROR_WORKLOAD), groups)

        for event in LogEvent.objects.exclude(log_file=''):
            self.assertTrue(present(log_path(event)).read_text().strip())

        stopped = Test.objects.get(dev__name=BENCH_MISMATCH_WORKLOAD)
        self.assertEqual(diagnose_workload(stopped).state, DiagnosisState.STOPPED_BY_ERROR)
        failing = Test.objects.get(dev__name=BUILD_FAILURE_WORKLOAD)
        self.assertEqual(diagnose_workload(failing).state, DiagnosisState.FAILING)

        actions = LogEvent.objects.filter(machine_id=0)
        self.assertEqual(actions.filter(summary__startswith='CREATE').count(), Test.objects.count())
        self.assertTrue(any(row.count > 1 for row in action_rows(list(operator_events()), now)))


def seeded_error_rows(now):
    rows, _ = group_rows(ErrorQuery(), now)
    return with_affected(rows, error_events(ErrorQuery()))


class ChainWorkloadTests(SimpleTestCase):
    def chain(self, *accepted: bool) -> list[tuple[str, str, str]]:
        commits = [
            DemoCommit(f'commit {index}', 5.0, (DemoStage(STC, 'passed'), DemoStage(LTC, 'passed')), accepted=accept)
            for index, accept in enumerate(accepted)
        ]
        return [
            (spec.info.split('\n')[0], spec.dev_sha, spec.base_sha) for spec in chain_workloads(commits, root='root')
        ]

    def test_an_accepted_commit_becomes_the_next_base(self):
        root, first, second = commit_sha('root'), commit_sha('commit 0'), commit_sha('commit 1')
        self.assertEqual(
            self.chain(True, True),
            [
                ('commit 0', first, root),
                ('commit 0', first, root),
                ('commit 1', second, first),
                ('commit 1', second, first),
            ],
        )

    def test_candidates_after_a_rejected_commit_share_its_base(self):
        bases = [base for _, _, base in self.chain(True, False, False)]
        self.assertEqual(bases, [commit_sha('root')] * 2 + [commit_sha('commit 0')] * 4)

    def test_each_stage_is_one_sprt_named_by_its_commits(self):
        commit = DemoCommit('subject', 2.0, (DemoStage(STC, 'passed'), DemoStage(LTC, 'active', pairs=10, hash_mb=64)))
        stc, ltc = chain_workloads([commit])
        self.assertEqual((stc.name, stc.base_name), (stc.dev_sha, stc.base_sha))
        self.assertEqual(stc.info, f'subject\navl:{stc.dev_sha[:12]}')
        self.assertRegex(stc.name, '^[0-9a-f]{40}$')
        self.assertEqual((stc.mode, stc.tc, stc.state, stc.hash_mb), ('SPRT', STC, 'passed', 0))
        self.assertEqual((ltc.mode, ltc.tc, ltc.state, ltc.hash_mb, ltc.pairs), ('SPRT', LTC, 'active', 64, 10))
        self.assertGreater(stc.days_ago, ltc.days_ago)
        self.assertLess(stc.duration_hours, 24 * (stc.days_ago - ltc.days_ago))
        self.assertEqual(ltc.duration_hours, 24 * ltc.days_ago)

    def test_stages_are_created_oldest_first(self):
        ages = [spec.days_ago for spec in chain_workloads(COMMIT_CHAIN)]
        self.assertEqual(ages, sorted(ages, reverse=True))
        self.assertGreater(min(ages), 0)
        self.assertLess(max(ages), 6)


class SeededLineageTests(TestCase):
    def setUp(self):
        temporary_media(self)

    @override_settings(DEBUG=True)
    def test_the_commit_chain_gives_the_progress_page_a_lineage(self):
        call_command('seed_demo', stdout=io.StringIO())

        lineage = present(progress_report(Window.ALL, 'Avalanche').lineage)
        accepted = [commit.subject for commit in COMMIT_CHAIN if commit.accepted]
        self.assertEqual([row.step.subject for row in lineage.steps], [*accepted, COMMIT_CHAIN[3].subject])
        self.assertEqual(lineage.origin.sha, commit_sha(CHAIN_ROOT))
        self.assertEqual(lineage.classes, [TimeClass.STC, TimeClass.LTC])
        self.assertEqual([len(row.candidates) for row in lineage.steps], [1, 1, 0])

        repeated = present(lineage.steps[1].step.measurement(TimeClass.STC))
        self.assertEqual(len(repeated.runs), 2)
        running = present(lineage.steps[2].step.measurement(TimeClass.LTC))
        self.assertTrue(running.provisional)
        stc, ltc = lineage.series
        self.assertEqual((stc.measured, stc.provisional), (3, 0))
        self.assertEqual((ltc.measured, ltc.provisional), (2, 1))

        (check,) = lineage.direct
        self.assertEqual((check.time_class, check.first_index, check.last_index), (TimeClass.LTC, 1, 2))
        self.assertEqual((check.measured, check.steps), (2, 2))
        self.assertTrue(lineage.detached)

    @override_settings(DEBUG=True)
    def test_the_commit_chain_slows_down_as_seeded(self):
        call_command('seed_demo', stdout=io.StringIO())

        report = progress_report(Window.ALL, 'Avalanche')
        economics = present(report.economics)
        on_trunk = [COMMIT_CHAIN[0], COMMIT_CHAIN[2], COMMIT_CHAIN[3]]
        for point, commit in zip(economics.speed.points, on_trunk, strict=True):
            speed = present(point.step)
            self.assertAlmostEqual(speed.ratio, commit.speed, delta=SPEED_HOST_NOISE)
            self.assertGreater(speed.hosts, 1)
        total = present(economics.speed.total)
        self.assertLess(present(total.upper), 1.0)
        self.assertEqual(economics.counter_coverage, 1.0)
        self.assertEqual((economics.trunk.steps, economics.failed.steps, economics.other.steps), (3, 1, 1))
        stc, ltc = economics.classes
        self.assertGreater(ltc.core_hours / ltc.games, 4 * stc.core_hours / stc.games)
