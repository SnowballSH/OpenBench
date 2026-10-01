import math
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from django.test import SimpleTestCase

from OpenBench.insights.domain import Outcomes
from OpenBench.progress.conditions import time_class
from OpenBench.progress.domain import (
    CANDIDATES_SENT,
    Commit,
    Lineage,
    RunMode,
    RunRow,
    RunStatus,
    Step,
    TimeClass,
)
from OpenBench.progress.lineage import (
    build_lineage,
    build_steps,
    chain,
    chain_series,
    half_width,
    lineage_report,
    summarize_lineage,
)
from OpenBench.stats import Elo
from OpenBench.tests.fixtures import present

START = datetime(2026, 9, 1, tzinfo=UTC)
PENTA = (5, 40, 100, 45, 10)
STRONG = (2, 20, 100, 60, 18)
WEAK = (12, 50, 100, 35, 4)


class Graph:
    def __init__(self) -> None:
        self.rows: list[RunRow] = []

    def run(
        self,
        base: str,
        dev: str,
        status: RunStatus = RunStatus.PASSED,
        time_class: TimeClass = TimeClass.STC,
        penta: tuple[int, int, int, int, int] = PENTA,
        mode: RunMode = RunMode.SPRT,
        use_penta: bool = True,
    ) -> RunRow:
        id = len(self.rows) + 1
        created = START + timedelta(days=id)
        finished = status not in (RunStatus.RUNNING, RunStatus.PENDING)
        wins, losses = 2 * penta[4] + penta[3], 2 * penta[0] + penta[1]
        row = RunRow(
            id=id,
            engine='Avalanche',
            repo='https://github.com/SnowballSH/Avalanche',
            base=Commit(base),
            dev=Commit(dev),
            subject=f'{base} to {dev}',
            author='author',
            mode=mode,
            status=status,
            time_class=time_class,
            time_control='8.0+0.08',
            created_at=created,
            finished_at=created + timedelta(hours=2) if finished else None,
            games=2 * sum(penta),
            outcomes=Outcomes((losses, 2 * sum(penta) - wins - losses, wins), penta, use_penta),
        )
        self.rows.append(row)
        return row

    def lineage(self) -> Lineage:
        return present(build_lineage(build_steps(self.rows)))


def path(steps: list[Step]) -> list[str]:
    return [f'{step.base.sha}>{step.dev.sha}' for step in steps]


class TimeClassTests(SimpleTestCase):
    def classify(
        self, control: str, threads: int = 1, base_control: str | None = None, base_threads: int | None = None
    ):
        return time_class(
            control,
            control if base_control is None else base_control,
            f'Threads={threads} Hash=16',
            f'Threads={threads if base_threads is None else base_threads} Hash=64',
        )

    def test_fischer_controls_are_bucketed_by_base_time(self):
        expected = {
            '8.0+0.08': TimeClass.STC,
            '19.9+0.20': TimeClass.STC,
            '20.0+0.20': TimeClass.LTC,
            '40.0+0.40': TimeClass.LTC,
            '119.9+1.00': TimeClass.LTC,
            '120.0+1.00': TimeClass.VLTC,
        }
        for control, bucket in expected.items():
            self.assertEqual(self.classify(control), bucket, control)

    def test_several_threads_are_their_own_class(self):
        self.assertEqual(self.classify('8.0+0.08', threads=4), TimeClass.SMP)
        self.assertEqual(self.classify('40.0+0.40', threads=2), TimeClass.SMP)

    def test_everything_else_is_other(self):
        for control in ('N=5000', 'D=10', 'MT=100', '40/10.0+0.00'):
            self.assertEqual(self.classify(control), TimeClass.OTHER, control)
        self.assertEqual(self.classify('8.0+0.08', base_control='10.0+0.10'), TimeClass.OTHER)
        self.assertEqual(self.classify('8.0+0.08', threads=1, base_threads=2), TimeClass.OTHER)
        self.assertEqual(time_class('8.0+0.08', '8.0+0.08', 'Hash=16', 'Hash=16'), TimeClass.OTHER)
        self.assertEqual(time_class('fast', 'fast', 'Threads=1', 'Threads=1'), TimeClass.OTHER)

    def test_only_other_is_left_out_of_chains(self):
        self.assertEqual([found for found in TimeClass if not found.chained], [TimeClass.OTHER])


class StepTests(SimpleTestCase):
    def test_runs_of_a_pair_are_one_step_with_a_measurement_per_class(self):
        graph = Graph()
        graph.run('a', 'b')
        graph.run('a', 'b', time_class=TimeClass.LTC, penta=STRONG)
        (step,) = build_steps(graph.rows)
        self.assertEqual([found.time_class for found in step.measurements], [TimeClass.STC, TimeClass.LTC])
        self.assertAlmostEqual(present(present(step.measurement(TimeClass.LTC)).elo).value, Elo(STRONG)[1])
        self.assertIsNone(step.measurement(TimeClass.VLTC))
        self.assertEqual((step.first_run, step.subject), (1, 'a to b'))

    def test_the_subject_is_the_first_line_of_the_first_described_run(self):
        graph = Graph()
        for subject in ('', '\nCorrection history, STC\navl:6b10ec947ac0', 'Correction history, LTC confirmation'):
            row = graph.run('a', 'b')
            graph.rows[-1] = replace(row, subject=subject)
        self.assertEqual(build_steps(graph.rows)[0].subject, 'Correction history, STC')

    def test_repeats_at_one_class_pool_their_counts(self):
        graph = Graph()
        graph.run('a', 'b', RunStatus.FAILED, penta=WEAK)
        graph.run('a', 'b', RunStatus.PASSED, penta=STRONG)
        (step,) = build_steps(graph.rows)
        (measurement,) = step.measurements
        pooled = tuple(a + b for a, b in zip(WEAK, STRONG, strict=True))
        self.assertEqual([run.id for run in measurement.runs], [1, 2])
        self.assertEqual(measurement.games, 2 * sum(pooled))
        self.assertAlmostEqual(present(measurement.elo).value, Elo(pooled)[1])
        self.assertLess(half_width(present(measurement.elo)), half_width(present(measurement.runs[0].elo)))
        self.assertEqual(measurement.verdict, RunStatus.PASSED)

    def test_the_newest_decided_run_gives_the_verdict(self):
        graph = Graph()
        graph.run('a', 'b', RunStatus.PASSED)
        graph.run('a', 'b', RunStatus.FAILED)
        graph.run('a', 'b', RunStatus.RUNNING)
        self.assertEqual(build_steps(graph.rows)[0].measurements[0].verdict, RunStatus.FAILED)

    def test_undecided_runs_report_the_most_telling_state(self):
        for statuses, verdict in (
            ((RunStatus.STOPPED, RunStatus.RUNNING), RunStatus.RUNNING),
            ((RunStatus.COMPLETED, RunStatus.PENDING), RunStatus.PENDING),
            ((RunStatus.STOPPED, RunStatus.COMPLETED), RunStatus.COMPLETED),
            ((RunStatus.STOPPED,), RunStatus.STOPPED),
        ):
            graph = Graph()
            for status in statuses:
                graph.run('a', 'b', status)
            self.assertEqual(build_steps(graph.rows)[0].measurements[0].verdict, verdict)

    def test_a_trinomial_run_makes_the_pool_trinomial(self):
        graph = Graph()
        first = graph.run('a', 'b')
        second = graph.run('a', 'b', use_penta=False)
        measurement = build_steps(graph.rows)[0].measurements[0]
        pooled = tuple(a + b for a, b in zip(first.outcomes.trinomial, second.outcomes.trinomial, strict=True))
        self.assertEqual(measurement.pooling.value, 'trinomial')
        self.assertAlmostEqual(present(measurement.elo).value, Elo(pooled)[1])

    def test_a_commit_against_itself_is_not_a_step(self):
        graph = Graph()
        graph.run('a', 'a', RunStatus.COMPLETED, mode=RunMode.GAMES)
        self.assertEqual(build_steps(graph.rows), [])
        self.assertIsNone(build_lineage([]))

    def test_networks_tell_commits_apart(self):
        graph = Graph()
        row = graph.run('a', 'a')
        graph.rows[0] = replace(row, dev=Commit('a', 'NET2'))
        self.assertEqual(len(build_steps(graph.rows)), 1)

    def test_too_few_games_leave_a_measurement_without_an_estimate(self):
        graph = Graph()
        graph.run('a', 'b', RunStatus.RUNNING, penta=(0, 0, 1, 0, 0))
        self.assertIsNone(build_steps(graph.rows)[0].measurements[0].elo)


class TrunkTests(SimpleTestCase):
    def test_a_linear_chain_is_the_trunk(self):
        graph = Graph()
        for base, dev in ('ab', 'bc', 'cd'):
            graph.run(base, dev)
        lineage = graph.lineage()
        self.assertEqual(path(lineage.trunk), ['a>b', 'b>c', 'c>d'])
        self.assertEqual((lineage.root.sha, lineage.head.sha), ('a', 'd'))
        self.assertEqual((lineage.branches, lineage.direct, lineage.detached), ({}, [], []))

    def test_the_trunk_stops_before_a_tip_that_has_not_passed(self):
        for status in (RunStatus.FAILED, RunStatus.RUNNING, RunStatus.STOPPED, RunStatus.PENDING):
            graph = Graph()
            graph.run('a', 'b')
            graph.run('b', 'c', status)
            lineage = graph.lineage()
            self.assertEqual(path(lineage.trunk), ['a>b'], status)
            self.assertEqual(path([found.step for found in lineage.branches[Commit('b')]]), ['b>c'])

    def test_a_failed_step_is_accepted_once_the_chain_continues_from_it(self):
        graph = Graph()
        graph.run('a', 'b', RunStatus.FAILED)
        graph.run('b', 'c')
        self.assertEqual(path(graph.lineage().trunk), ['a>b', 'b>c'])

    def test_a_tip_that_passed_one_class_and_failed_another_is_not_accepted(self):
        graph = Graph()
        graph.run('a', 'b')
        graph.run('b', 'c')
        graph.run('b', 'c', RunStatus.FAILED, TimeClass.LTC)
        self.assertEqual(path(graph.lineage().trunk), ['a>b'])

    def test_parallel_siblings_branch_off_and_never_join_the_trunk(self):
        graph = Graph()
        graph.run('a', 'b')
        graph.run('b', 'x', RunStatus.FAILED)
        graph.run('b', 'y')
        graph.run('b', 'c')
        graph.run('c', 'd')
        lineage = graph.lineage()
        self.assertEqual(path(lineage.trunk), ['a>b', 'b>c', 'c>d'])
        self.assertEqual(path([found.step for found in lineage.branches[Commit('b')]]), ['b>x', 'b>y'])
        self.assertEqual(lineage.detached, [])

    def test_an_abandoned_branch_keeps_its_depth(self):
        graph = Graph()
        graph.run('a', 'b')
        graph.run('b', 'x')
        graph.run('x', 'y', RunStatus.FAILED)
        graph.run('b', 'c')
        graph.run('c', 'd')
        lineage = graph.lineage()
        self.assertEqual(path(lineage.trunk), ['a>b', 'b>c', 'c>d'])
        self.assertEqual(
            [(found.depth, *path([found.step])) for found in lineage.branches[Commit('b')]], [(1, 'b>x'), (2, 'x>y')]
        )

    def test_a_stray_test_off_an_old_commit_does_not_cut_the_trunk_short(self):
        graph = Graph()
        for base, dev in ('ab', 'bc', 'cd'):
            graph.run(base, dev)
        graph.run('b', 'x', RunStatus.FAILED)
        self.assertEqual(path(graph.lineage().trunk), ['a>b', 'b>c', 'c>d'])

    def test_the_trunk_follows_the_branch_the_newest_test_builds_on(self):
        graph = Graph()
        graph.run('a', 'b')
        graph.run('b', 'c')
        graph.run('c', 'd')
        graph.run('b', 'x')
        graph.run('x', 'y', RunStatus.RUNNING)
        lineage = graph.lineage()
        self.assertEqual(path(lineage.trunk), ['a>b', 'b>x'])
        self.assertEqual(path([found.step for found in lineage.branches[Commit('b')]]), ['b>c', 'c>d'])

    def test_two_roots_leave_the_older_tree_detached(self):
        graph = Graph()
        graph.run('p', 'q')
        graph.run('q', 'r')
        graph.run('a', 'b')
        graph.run('b', 'c')
        lineage = graph.lineage()
        self.assertEqual(path(lineage.trunk), ['a>b', 'b>c'])
        self.assertEqual(path(lineage.detached), ['p>q', 'q>r'])

    def test_a_tree_without_an_accepted_step_does_not_take_the_trunk(self):
        graph = Graph()
        graph.run('a', 'b')
        graph.run('p', 'q', RunStatus.FAILED)
        lineage = graph.lineage()
        self.assertEqual(path(lineage.trunk), ['a>b'])
        self.assertEqual(path(lineage.detached), ['p>q'])

    def test_nothing_accepted_leaves_an_empty_trunk_at_the_newest_base(self):
        graph = Graph()
        graph.run('a', 'b', RunStatus.FAILED)
        graph.run('a', 'c', RunStatus.RUNNING)
        lineage = graph.lineage()
        self.assertEqual((lineage.trunk, lineage.root, lineage.head), ([], Commit('a'), Commit('a')))
        self.assertEqual(path([found.step for found in lineage.branches[Commit('a')]]), ['a>b', 'a>c'])

    def test_a_run_against_an_older_trunk_commit_is_a_direct_check_not_a_step(self):
        graph = Graph()
        for base, dev in ('ab', 'bc', 'cd'):
            graph.run(base, dev)
        graph.run('a', 'd', RunStatus.COMPLETED, mode=RunMode.GAMES)
        lineage = graph.lineage()
        self.assertEqual(path(lineage.trunk), ['a>b', 'b>c', 'c>d'])
        (direct,) = lineage.direct
        self.assertEqual((direct.first_index, direct.last_index, *path([direct.step])), (1, 3, 'a>d'))
        self.assertEqual(lineage.detached, [])

    def test_the_longest_chain_is_the_parent_whatever_the_test_order(self):
        graph = Graph()
        graph.run('a', 'd', RunStatus.COMPLETED, mode=RunMode.GAMES)
        for base, dev in ('ab', 'bc', 'cd'):
            graph.run(base, dev)
        lineage = graph.lineage()
        self.assertEqual(path(lineage.trunk), ['a>b', 'b>c', 'c>d'])
        self.assertEqual(path([run.step for run in lineage.direct]), ['a>d'])

    def test_a_cycle_is_broken_and_its_closing_test_is_detached(self):
        graph = Graph()
        graph.run('a', 'b')
        graph.run('b', 'c')
        graph.run('c', 'a')
        lineage = graph.lineage()
        self.assertEqual(path(lineage.trunk), ['a>b', 'b>c'])
        self.assertEqual(path(lineage.detached), ['c>a'])

    def test_a_long_chain_does_not_recurse(self):
        graph = Graph()
        for index in range(3000):
            graph.run(str(index), str(index + 1))
        self.assertEqual(len(graph.lineage().trunk), 3000)


class ChainTests(SimpleTestCase):
    def chain_graph(self) -> Graph:
        graph = Graph()
        graph.run('a', 'b')
        graph.run('a', 'b', time_class=TimeClass.LTC, penta=STRONG)
        graph.run('b', 'c', penta=STRONG)
        graph.run('c', 'd')
        graph.run('c', 'd', time_class=TimeClass.LTC)
        return graph

    def test_variances_add_and_values_add(self):
        first, second = Elo(PENTA), Elo(STRONG)
        graph = self.chain_graph()
        series = chain_series(graph.lineage().trunk, TimeClass.STC, 1)
        margin = math.sqrt(2 * ((first[2] - first[0]) / 2) ** 2 + ((second[2] - second[0]) / 2) ** 2)
        self.assertAlmostEqual(present(series.total).value, 2 * first[1] + second[1])
        self.assertAlmostEqual(half_width(present(series.total)), margin)
        self.assertEqual((series.measured, series.steps), (3, 3))
        self.assertEqual([point.index for point in series.points], [1, 2, 3])
        self.assertAlmostEqual(present(series.points[1].cumulative).value, first[1] + second[1])

    def test_a_missing_class_is_a_gap_and_never_borrows_another_class(self):
        series = chain_series(self.chain_graph().lineage().trunk, TimeClass.LTC, 1)
        self.assertEqual((series.measured, series.steps), (2, 3))
        self.assertIsNone(series.points[1].elo)
        self.assertIsNone(series.points[1].cumulative)
        self.assertAlmostEqual(present(series.points[2].cumulative).value, Elo(STRONG)[1] + Elo(PENTA)[1])
        self.assertIsNone(chain_series(self.chain_graph().lineage().trunk, TimeClass.VLTC, 1).total)
        self.assertIsNone(chain([]))

    def test_classes_are_separate_series(self):
        report = lineage_report('Avalanche', self.chain_graph().lineage(), None)
        self.assertEqual([series.time_class for series in report.series], [TimeClass.STC, TimeClass.LTC])
        self.assertEqual(report.classes, [TimeClass.STC, TimeClass.LTC])

    def test_other_conditions_are_listed_but_never_chained(self):
        graph = Graph()
        graph.run('a', 'b', time_class=TimeClass.OTHER)
        graph.run('b', 'c', time_class=TimeClass.OTHER)
        report = lineage_report('Avalanche', graph.lineage(), None)
        self.assertEqual((report.classes, report.series), ([TimeClass.OTHER], []))

    def test_direct_checks_compare_the_same_span_at_the_same_class(self):
        graph = self.chain_graph()
        graph.run('a', 'd', RunStatus.COMPLETED, TimeClass.LTC, STRONG, RunMode.GAMES)
        graph.run('b', 'd', RunStatus.COMPLETED, TimeClass.STC, STRONG, RunMode.GAMES)
        whole, tail = lineage_report('Avalanche', graph.lineage(), None).direct
        self.assertEqual((whole.time_class, whole.first_index, whole.last_index), (TimeClass.LTC, 1, 3))
        self.assertEqual((whole.measured, whole.steps), (2, 3))
        self.assertAlmostEqual(present(whole.chained).value, Elo(STRONG)[1] + Elo(PENTA)[1])
        self.assertAlmostEqual(present(whole.direct.elo).value, Elo(STRONG)[1])
        self.assertEqual((tail.time_class, tail.first_index, tail.measured, tail.steps), (TimeClass.STC, 2, 2, 2))
        self.assertAlmostEqual(present(tail.chained).value, Elo(STRONG)[1] + Elo(PENTA)[1])


class WindowTests(SimpleTestCase):
    def test_the_window_shows_the_trunk_from_its_first_recent_step(self):
        graph = Graph()
        for base, dev in ('ab', 'bc', 'cd', 'de'):
            graph.run(base, dev)
        graph.run('b', 'x', RunStatus.FAILED)
        graph.run('a', 'e', RunStatus.COMPLETED, mode=RunMode.GAMES)
        graph.run('a', 'b', time_class=TimeClass.LTC)
        lineage = graph.lineage()
        since = START + timedelta(days=3)

        report = lineage_report('Avalanche', lineage, since)
        self.assertEqual([row.index for row in report.steps], [3, 4])
        self.assertEqual((report.origin, report.trunk_length), (Commit('c'), 4))
        self.assertEqual([point.index for point in report.series[0].points], [3, 4])
        self.assertAlmostEqual(present(report.series[0].total).value, 2 * Elo(PENTA)[1])
        self.assertEqual((report.direct[0].first_index, report.direct[0].steps), (1, 4))
        self.assertEqual(summarize_lineage(lineage, since).steps_accepted, 2)

        everything = lineage_report('Avalanche', lineage, None)
        self.assertEqual([row.index for row in everything.steps], [1, 2, 3, 4])
        self.assertEqual(path([found.step for found in everything.steps[0].candidates]), ['b>x'])
        summary = summarize_lineage(lineage, None)
        self.assertEqual((summary.steps_accepted, summary.candidates, summary.measurements, summary.runs), (4, 1, 6, 6))

    def test_a_window_without_steps_shows_the_head_and_its_candidates(self):
        graph = Graph()
        graph.run('a', 'b')
        graph.run('b', 'c', RunStatus.RUNNING)
        report = lineage_report('Avalanche', graph.lineage(), START + timedelta(days=30))
        self.assertEqual((report.steps, report.series, report.origin), ([], [], Commit('b')))
        self.assertEqual(path([found.step for found in report.origin_candidates]), ['b>c'])

    def test_long_candidate_lists_keep_the_newest(self):
        graph = Graph()
        graph.run('a', 'b')
        for index in range(CANDIDATES_SENT + 5):
            graph.run('b', f'x{index}', RunStatus.FAILED)
        graph.run('b', 'c')
        (row, _) = lineage_report('Avalanche', graph.lineage(), None).steps
        self.assertEqual((len(row.candidates), row.candidates_omitted), (CANDIDATES_SENT, 5))
        self.assertEqual(row.candidates[0].step.dev.sha, 'x5')
