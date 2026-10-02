import json
import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest import mock

from django.core.cache import cache
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from OpenBench.models import Engine, EngineConfig, Test
from OpenBench.progress.anchor import release_report
from OpenBench.progress.domain import ANCHOR_POINTS_SENT, RunMode, RunStatus, TimeClass, Window
from OpenBench.progress.present import release_page
from OpenBench.progress.report import progress_report
from OpenBench.releases import store
from OpenBench.releases.domain import REFRESH_INTERVAL, BranchStanding, Release, ReleaseAnchor
from OpenBench.stats import Elo
from OpenBench.tests.fixtures import (
    LTC_PRESET,
    PASSWORD,
    STC_PRESET,
    create_engine_config,
    create_test,
    create_user,
    credentials,
    ensure_book,
    present,
    set_test_presets,
)
from OpenBench.tests.test_progress_lineage import PENTA, START, STRONG, Graph
from OpenBench.workloads.release_measurement import (
    MeasurementError,
    load_measurement,
    measurement_options,
)

SOURCE = 'https://github.com/SnowballSH/Avalanche'
RELEASE = 'r' * 40
NOW = START + timedelta(days=30)
PREFILL = re.compile(r'<script id="json-prefill" type="application/json">(.*?)</script>', re.DOTALL)
DEFAULT = {
    'both_branch': 'master',
    'book_name': 'UHO_Lichess_4852_v1.epd',
    'test_bounds': '[0.00, 3.00]',
    'test_confidence': '[0.05, 0.05]',
    'test_max_games': 40000,
}


def anchor(**changes: Any) -> ReleaseAnchor:
    known = ReleaseAnchor(
        engine='Avalanche',
        tag='v4.0.0',
        sha=RELEASE,
        published_at=START,
        default_branch='master',
        pinned=False,
        fetched_at=NOW,
        attempted_at=NOW,
        error='',
    )
    return replace(known, **changes)


def games(run: Any, **changes: Any) -> Any:
    return replace(run, mode=RunMode.GAMES, **changes)


class AnchorSelectionTests(SimpleTestCase):
    def setUp(self) -> None:
        self.graph = Graph()

    def report(self, on_branch: dict[str, datetime | None] | None = None, release: ReleaseAnchor | None = None):
        chosen = release or anchor()
        return release_report('Avalanche', chosen, self.graph.rows, on_branch or {}, SOURCE)

    def measure(self, dev: str, status: RunStatus = RunStatus.COMPLETED, **fields: Any) -> None:
        self.graph.run(RELEASE, dev, status, mode=RunMode.GAMES, **fields)

    def test_only_runs_based_on_the_release_are_anchor_runs(self):
        self.measure('m1')
        self.graph.run('m1', 'm2')
        self.graph.run(RELEASE, RELEASE, RunStatus.COMPLETED)
        self.graph.run('m2', RELEASE)

        report = self.report({'m1': None})
        (series,) = report.series
        self.assertEqual([point.dev.sha for point in series.points], ['m1'])
        self.assertEqual(report.branches, [])

    def test_the_release_sha_matches_whatever_its_case(self):
        self.measure('m1')
        (series,) = self.report({'m1': None}, anchor(sha=RELEASE.upper())).series
        self.assertEqual(len(series.points), 1)

    def test_a_dev_not_known_to_be_on_the_default_branch_is_kept_apart(self):
        self.measure('m1')
        self.measure('feature', penta=STRONG)

        report = self.report({'m1': START})
        (series,) = report.series
        self.assertEqual(present(series.latest).dev.sha, 'm1')
        self.assertEqual(present(series.latest).committed_at, START)
        self.assertEqual([point.dev.sha for point in report.branches], ['feature'])

        merged = self.report({'m1': START, 'feature': None})
        self.assertEqual(present(merged.series[0].latest).dev.sha, 'feature')
        self.assertEqual(merged.branches, [])

    def test_a_test_built_from_the_branch_name_is_on_the_branch(self):
        self.measure('head')
        self.measure('fork')
        self.measure('other')
        head, fork, other = self.graph.rows
        self.graph.rows = [
            replace(head, dev_name='master', repo=f'{SOURCE}/'),
            replace(fork, dev_name='master', repo='https://github.com/someone/Avalanche'),
            replace(other, dev_name='main'),
        ]
        report = self.report()
        self.assertEqual([point.dev.sha for point in report.series[0].points], ['head'])
        self.assertEqual({point.dev.sha for point in report.branches}, {'fork', 'other'})

    def test_repeats_pool_and_classes_never_mix(self):
        self.measure('m1')
        self.measure('m1', penta=STRONG)
        self.measure('m1', time_class=TimeClass.LTC)

        stc, ltc = self.report({'m1': None}).series
        self.assertEqual((stc.time_class, ltc.time_class), (TimeClass.STC, TimeClass.LTC))
        pooled = present(stc.latest).measurement
        self.assertEqual(len(pooled.runs), 2)
        expected = Elo(tuple(left + right for left, right in zip(PENTA, STRONG, strict=True)))
        self.assertAlmostEqual(present(pooled.elo).value, expected[1])
        self.assertEqual(len(present(ltc.latest).measurement.runs), 1)

    def test_the_headline_is_the_newest_finished_measurement(self):
        self.measure('m1')
        self.measure('m2', penta=STRONG)
        self.measure('m3', RunStatus.RUNNING)

        (series,) = self.report({'m1': None, 'm2': None, 'm3': None}).series
        self.assertEqual([point.dev.sha for point in series.points], ['m1', 'm2', 'm3'])
        self.assertEqual(present(series.latest).dev.sha, 'm2')
        self.assertTrue(series.points[-1].measurement.provisional)

    def test_only_running_runs_leave_no_headline(self):
        self.measure('m1', RunStatus.RUNNING)
        (series,) = self.report({'m1': None}).series
        self.assertIsNone(series.latest)
        self.assertEqual(len(series.points), 1)

    def test_a_finished_run_outranks_a_running_repeat_of_the_same_commit(self):
        self.measure('m1')
        self.measure('m1', RunStatus.RUNNING, penta=STRONG)
        (series,) = self.report({'m1': None}).series
        measurement = present(series.latest).measurement
        self.assertFalse(measurement.provisional)
        self.assertAlmostEqual(present(measurement.elo).value, Elo(PENTA)[1])

    def test_an_sprt_against_the_release_is_listed_apart_and_never_pooled(self):
        self.measure('m1')
        self.graph.run(RELEASE, 'm1', RunStatus.PASSED, penta=STRONG)
        self.graph.run(RELEASE, 'm2', RunStatus.FAILED)

        report = self.report({'m1': None, 'm2': None})
        (series,) = report.series
        self.assertEqual([point.dev.sha for point in series.points], ['m1'])
        pooled = present(series.latest).measurement
        self.assertEqual(len(pooled.runs), 1)
        self.assertAlmostEqual(present(pooled.elo).value, Elo(PENTA)[1])
        self.assertEqual([point.dev.sha for point in report.sprt], ['m2', 'm1'])
        self.assertEqual(report.sprt[0].measurement.verdict, RunStatus.FAILED)
        self.assertEqual(report.branches, [])

    def test_the_headline_follows_commit_order_when_both_commits_are_dated(self):
        self.measure('newer', penta=STRONG)
        self.measure('older')
        dated: dict[str, datetime | None] = {'newer': START + timedelta(days=5), 'older': START + timedelta(days=2)}

        (series,) = self.report(dated).series
        self.assertEqual([point.dev.sha for point in series.points], ['newer', 'older'])
        self.assertEqual(present(series.latest).dev.sha, 'newer')

    def test_the_headline_falls_back_to_measurement_order_when_a_commit_is_undated(self):
        self.measure('newer', penta=STRONG)
        self.measure('older')
        undated: tuple[dict[str, datetime | None], ...] = (
            {'newer': START + timedelta(days=5), 'older': None},
            {'newer': None, 'older': None},
        )
        for dated in undated:
            (series,) = self.report(dated).series
            self.assertEqual(present(series.latest).dev.sha, 'older')

    def test_a_remeasured_commit_keeps_its_one_pooled_point(self):
        self.measure('old')
        self.measure('new', penta=STRONG)
        self.measure('old')
        dated: dict[str, datetime | None] = {'old': START, 'new': START + timedelta(days=1)}
        (series,) = self.report(dated).series
        self.assertEqual(present(series.latest).dev.sha, 'new')
        self.assertEqual(len(series.points[-1].measurement.runs), 2)

    def test_newer_bases_count_the_bases_first_tested_after_the_measurement(self):
        self.graph.run('b0', 'f0')
        self.measure('m1')
        self.graph.run('b1', 'f1')
        self.graph.run('b1', 'f2')
        self.graph.run('b2', 'f3')
        self.graph.run('m1', 'f4')
        self.graph.run('b0', 'f5')
        self.measure('m2')

        (series,) = self.report({'m1': None, 'm2': None}).series
        self.assertEqual([point.newer_bases for point in series.points], [2, 0])

    def test_no_release_or_an_unknown_one_measures_nothing(self):
        self.measure('m1')
        missing = release_report('Avalanche', None, self.graph.rows, {'m1': None}, SOURCE)
        self.assertEqual((missing.anchor, missing.series, missing.branches), (None, [], []))
        unknown = self.report({'m1': None}, anchor(tag='', sha=''))
        self.assertEqual((unknown.series, unknown.branches), ([], []))

    def test_no_anchor_run_is_an_empty_report(self):
        self.graph.run('a', 'b')
        report = self.report()
        self.assertEqual((report.series, report.branches), ([], []))
        self.assertTrue(present(report.anchor).known)

    def test_the_series_sent_are_bounded(self):
        commits = [f'm{index}' for index in range(ANCHOR_POINTS_SENT + 5)]
        for commit in commits:
            self.measure(commit)
        report = self.report(dict.fromkeys(commits))
        self.assertEqual(len(report.series[0].points), ANCHOR_POINTS_SENT)
        self.assertEqual(report.points_omitted, 5)
        self.assertEqual(report.series[0].points[-1].dev.sha, commits[-1])


class ReleasePageTests(SimpleTestCase):
    def page(self, release: ReleaseAnchor | None, graph: Graph | None = None, on_branch: tuple[str, ...] = ()):
        rows = graph.rows if graph else []
        return release_page(release_report('Avalanche', release, rows, dict.fromkeys(on_branch), SOURCE), NOW)

    def test_no_anchor_run_says_so_and_shows_no_tiles(self):
        graph = Graph()
        graph.run('a', 'b')
        page = self.page(anchor(), graph)
        self.assertEqual((page.title, page.known, page.tiles, page.lines), ('Since v4.0.0', True, [], []))
        self.assertEqual((page.tag, page.branch), ('v4.0.0', 'master'))
        self.assertEqual(present(page.commit).url, f'{SOURCE}/commit/{RELEASE}')

    def test_tiles_carry_the_measurement_and_its_staleness(self):
        graph = Graph()
        graph.run(RELEASE, 'm1', RunStatus.COMPLETED, mode=RunMode.GAMES)
        graph.run('b1', 'f1')
        graph.run(RELEASE, 'm2', RunStatus.RUNNING, mode=RunMode.GAMES)
        stc, ltc = self.page(anchor(), graph, ('m1', 'm2')).tiles
        low, value, high = Elo(PENTA)
        self.assertEqual(stc.label, 'STC Elo vs release')
        self.assertEqual(stc.value, f'+{value:.1f} ± {(high - low) / 2:.1f}')
        self.assertEqual((stc.detail, stc.tone), ('400 games', 'pass'))
        self.assertEqual(present(stc.commit).label, 'm1')
        self.assertEqual([run.url for run in stc.runs], ['/test/1/'])
        self.assertEqual(
            stc.facts,
            [
                'measured Sep 2, 2026',
                'master has moved: 1 newer base commit tested since',
                '1 newer test still running',
            ],
        )
        self.assertEqual((ltc.value, ltc.detail, ltc.facts), ('—', 'not measured at LTC', []))

        committed = {'m1': START - timedelta(days=3)}
        report = release_report('Avalanche', anchor(), graph.rows[:1], committed, SOURCE)
        dated, _ = release_page(report, NOW).tiles
        self.assertEqual(
            dated.facts, ['committed Aug 29, 2026 · measured Sep 2, 2026', 'no newer base has been tested since']
        )

    def test_lines_are_newest_first_with_a_diff_from_the_release(self):
        graph = Graph()
        graph.run(RELEASE, 'm1', RunStatus.COMPLETED, mode=RunMode.GAMES)
        graph.run(RELEASE, 'm2', RunStatus.COMPLETED, mode=RunMode.GAMES)
        graph.run(RELEASE, 'feature', RunStatus.COMPLETED, mode=RunMode.GAMES)
        page = self.page(anchor(), graph, ('m1', 'm2'))
        self.assertEqual([line.commit.label for line in page.lines], ['m2', 'm1'])
        self.assertEqual(page.lines[0].compare_url, f'{SOURCE}/compare/{RELEASE}...m2')
        self.assertEqual(page.lines[0].row_url, '/test/2/')
        self.assertEqual([line.commit.label for line in page.branches], ['feature'])

    def test_networks_are_named_only_when_they_tell_rows_apart(self):
        graph = Graph()
        graph.run(RELEASE, 'm1', RunStatus.COMPLETED, mode=RunMode.GAMES)
        self.assertEqual(self.page(anchor(), graph, ('m1',)).lines[0].networks, '')

        graph.run(RELEASE, 'm1', RunStatus.COMPLETED, mode=RunMode.GAMES)
        same, other = graph.rows
        graph.rows = [same, replace(other, base=replace(other.base, network='OLDNET01'))]
        lines = self.page(anchor(), graph, ('m1',)).lines
        self.assertEqual(
            sorted(line.networks for line in lines),
            ['no network against the release with OLDNET01', 'no network against the release with no network'],
        )

    def test_sprt_runs_get_their_own_lines(self):
        graph = Graph()
        graph.run(RELEASE, 'm1', RunStatus.PASSED)
        page = self.page(anchor(), graph, ('m1',))
        self.assertEqual((page.tiles, page.lines), ([], []))
        self.assertEqual([line.cell.verdict for line in page.sprt], ['passed'])

    def test_the_base_note_says_whether_the_release_bench_is_recorded(self):
        missing = self.page(anchor())
        self.assertFalse(missing.base_recorded)
        self.assertIn('set_release Avalanche --bench <nodes> --network <sha or none>', missing.base_note)

        recorded = self.page(anchor(bench=3141592, network='none'))
        self.assertTrue(recorded.base_recorded)
        self.assertEqual(
            recorded.base_note, 'Release bench 3,141,592 recorded, no network; the create form fills them in.'
        )
        self.assertIn('network ABCDEF01', self.page(anchor(bench=5, network='ABCDEF01')).base_note)
        self.assertIn('network not recorded', self.page(anchor(bench=5)).base_note)

    def test_status_tells_how_old_the_release_is(self):
        fresh = self.page(anchor(fetched_at=NOW - timedelta(hours=2)))
        self.assertEqual((fresh.status, fresh.stale), ('checked with GitHub 2.0 h ago', False))

        overdue = self.page(anchor(fetched_at=NOW - 3 * REFRESH_INTERVAL))
        self.assertTrue(overdue.stale)

        failing = self.page(
            anchor(
                fetched_at=NOW - timedelta(days=3),
                attempted_at=NOW - timedelta(minutes=30),
                error='GitHub rate limit reached',
            )
        )
        self.assertTrue(failing.stale)
        self.assertEqual(
            failing.status,
            'GitHub lookup failing (GitHub rate limit reached); '
            'the release shown was confirmed 3.0 d ago, last asked 30 min ago',
        )
        self.assertTrue(failing.known)

        pinned = self.page(anchor(pinned=True, attempted_at=None))
        self.assertEqual((pinned.status, pinned.stale), ('set by the operator; GitHub is not asked', False))

    def test_an_unknown_release_explains_itself(self):
        never = self.page(None)
        self.assertEqual((never.title, never.known), ('Since the latest release', False))
        self.assertIn('has not been looked up yet', never.explanation)

        failed = self.page(anchor(tag='', sha='', fetched_at=None, error='GitHub answered 502'))
        self.assertFalse(failed.known)
        self.assertIn('could not be looked up: GitHub answered 502', failed.explanation)
        self.assertTrue(failed.stale)

        none = self.page(anchor(tag='', sha=''))
        self.assertIn('no published release', none.explanation)


class MeasurementPrefillTests(TestCase):
    def setUp(self) -> None:
        self.config = set_test_presets(create_engine_config(), DEFAULT, STC=STC_PRESET, LTC=LTC_PRESET)
        store.pin_release('Avalanche', Release('v4.0.0', RELEASE, None), 'master', timezone.now())

    def test_options_cover_the_classes_the_engine_defines(self):
        stc, ltc = measurement_options(self.config)
        self.assertEqual((stc.label, stc.preset, stc.games, stc.games_text), ('STC', 'STC', 10_000, '10,000'))
        self.assertEqual((ltc.label, ltc.preset, ltc.games), ('LTC', 'LTC', 5_000))
        self.assertEqual(stc.url, '/test/new/?release=Avalanche&preset=STC')
        self.assertEqual(ltc.time_control, '40.0+0.40')

        only_stc = set_test_presets(self.config, DEFAULT, STC=STC_PRESET)
        self.assertEqual([option.label for option in measurement_options(only_stc)], ['STC'])
        self.assertEqual(measurement_options(set_test_presets(self.config, DEFAULT)), [])

    def test_a_fixed_games_preset_of_the_class_is_preferred_and_sets_the_count(self):
        release_ltc = {**LTC_PRESET, 'test_max_games': 3000}
        config = set_test_presets(self.config, DEFAULT, STC=STC_PRESET, LTC=LTC_PRESET, **{'release-ltc': release_ltc})
        stc, ltc = measurement_options(config)
        self.assertEqual((stc.preset, stc.games), ('STC', 10_000))
        self.assertEqual((ltc.preset, ltc.games), ('release-ltc', 3000))

    def test_the_prefill_is_a_fixed_games_test_of_the_branch_against_the_tag(self):
        prefill = load_measurement('Avalanche', 'LTC')
        self.assertEqual((prefill.tag, prefill.default_branch, prefill.games), ('v4.0.0', 'master', 5000))
        self.assertEqual(
            prefill.fields,
            {
                'dev_engine': 'Avalanche',
                'base_engine': 'Avalanche',
                'dev_repo': SOURCE,
                'base_repo': SOURCE,
                'dev_branch': 'master',
                'base_branch': 'v4.0.0',
                'dev_options': 'Threads=1 Hash=64',
                'base_options': 'Threads=1 Hash=64',
                'dev_time_control': '40.0+0.40',
                'base_time_control': '40.0+0.40',
                'book_name': 'UHO_Lichess_4852_v1.epd',
                'workload_size': '8',
                'priority': '2',
                'info': 'master against the release v4.0.0',
                'test_mode': 'GAMES',
                'test_bounds': 'N/A',
                'test_confidence': 'N/A',
                'test_max_games': '5000',
            },
        )
        self.assertEqual(len(prefill.still_to_type), 3)
        self.assertIn("Dev Bench (the bench of master's head", prefill.still_to_type[0])
        self.assertIn(
            'Base Bench (the bench of v4.0.0; record it with set_release Avalanche --bench)', prefill.still_to_type
        )

    def test_a_recorded_bench_and_network_are_filled_in_and_no_longer_asked_for(self):
        store.set_metadata('Avalanche', 3141592, 'ABCDEF01')
        prefill = load_measurement('Avalanche', 'STC')
        self.assertEqual((prefill.fields['base_bench'], prefill.fields['base_network']), ('3141592', 'ABCDEF01'))
        self.assertNotIn('dev_bench', prefill.fields)
        self.assertEqual(len(prefill.still_to_type), 1)

        store.set_metadata('Avalanche', None, 'none')
        self.assertEqual(load_measurement('Avalanche', 'STC').fields['base_network'], '')

    def test_a_rejected_submission_keeps_the_notice_and_what_was_typed(self):
        ensure_book()
        create_user('creator')
        self.client.post('/login/', {'username': 'creator', 'password': PASSWORD})
        prefill = load_measurement('Avalanche', 'STC')
        typed = {'dev_branch': '', 'base_branch': '', 'dev_bench': '123'}
        submitted = {**prefill.fields, 'release_of': 'Avalanche', 'release_preset': 'STC', **typed}

        with mock.patch('OpenBench.workloads.verify_workload.requests.get') as get:
            response = self.client.post('/test/new/', submitted)
        get.assert_not_called()
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="release-notice"')
        self.assertContains(response, 'name="release_of" value="Avalanche"')
        self.assertContains(response, 'name="release_preset" value="STC"')
        fields = json.loads(present(PREFILL.search(response.content.decode())).group(1))
        self.assertEqual((fields['dev_bench'], fields['test_max_games']), ('123', '10000'))
        self.assertFalse(Test.objects.exists())

    def test_nothing_is_filled_in_without_an_engine_a_release_or_a_preset(self):
        for engine, preset in (
            ('Missing', 'STC'),
            ('Avalanche', 'Missing'),
            ('Avalanche', None),
            ('Avalanche', 'default'),
        ):
            with self.subTest(engine=engine, preset=preset), self.assertRaises(MeasurementError):
                load_measurement(engine, preset)
        store.record_release('Avalanche', None, 'master', timezone.now())
        with self.assertRaisesRegex(MeasurementError, 'no release of Avalanche is known'):
            load_measurement('Avalanche', 'STC')

    def test_the_create_form_is_prefilled_and_creates_nothing(self):
        ensure_book()
        creator = create_user('creator')
        self.client.post('/login/', {'username': 'creator', 'password': PASSWORD})

        response = self.client.get('/test/new/?release=Avalanche&preset=STC')
        self.assertEqual(response.status_code, 200)
        fields = json.loads(present(PREFILL.search(response.content.decode())).group(1))
        self.assertEqual((fields['dev_branch'], fields['base_branch']), ('master', 'v4.0.0'))
        self.assertEqual((fields['test_mode'], fields['test_max_games']), ('GAMES', '10000'))
        self.assertContains(response, 'id="release-notice"')
        self.assertContains(response, '10,000 fixed games at the STC preset, not an SPRT')
        self.assertContains(response, 'Still to type before submitting: Dev Bench')
        self.assertContains(response, 'name="release_of" value="Avalanche"')
        self.assertNotContains(response, 'name="clone_of"')
        self.assertFalse(Test.objects.exists())
        self.assertEqual(creator.username, 'creator')

        missing = self.client.get('/test/new/?release=Avalanche&preset=Missing')
        self.assertEqual(json.loads(present(PREFILL.search(missing.content.decode())).group(1)), None)
        self.assertNotContains(missing, 'id="release-notice"')

        tune = self.client.get('/tune/new/?release=Avalanche&preset=STC')
        self.assertNotContains(tune, 'id="release-notice"')


class ReleaseViewTests(TestCase):
    def setUp(self) -> None:
        ensure_book()
        self.config = set_test_presets(create_engine_config(), DEFAULT, STC=STC_PRESET, LTC=LTC_PRESET)
        self.creator = create_user('creator')
        self.disabled = create_user('disabled', enabled=False)
        cache.clear()

    def login(self, username: str = 'creator') -> None:
        self.client.post('/login/', {'username': username, 'password': PASSWORD})

    def release(self) -> None:
        store.pin_release('Avalanche', Release('v4.0.0', RELEASE, None), 'master', timezone.now())

    def measured(self, dev_sha: str, **fields: Any) -> Test:
        counts = {
            'games': 400,
            'LL': 5,
            'LD': 40,
            'DD': 100,
            'DW': 45,
            'WW': 10,
            'wins': 65,
            'losses': 50,
            'draws': 285,
        }
        test = create_test(self.creator, **{'test_mode': 'GAMES', 'finished': True, **counts, **fields})
        Engine.objects.filter(id=test.dev_id).update(sha=dev_sha, name=dev_sha)
        Engine.objects.filter(id=test.base_id).update(sha=RELEASE, name='v4.0.0')
        return test

    def test_the_live_state_says_no_test_against_the_release_and_offers_the_action(self):
        self.release()
        create_test(self.creator, finished=True, passed=True, games=400, LL=5, LD=40, DD=100, DW=45, WW=10)
        self.login()
        response = self.client.get('/progress/Avalanche/')
        self.assertContains(response, 'Since v4.0.0')
        self.assertContains(response, 'No test of master against v4.0.0 yet.')
        self.assertContains(response, 'not progress since the release')
        self.assertContains(response, 'href="/test/new/?release=Avalanche&amp;preset=STC"')
        self.assertContains(response, 'href="/test/new/?release=Avalanche&amp;preset=LTC"')
        self.assertContains(response, 'STC · 10,000 games')
        self.assertContains(response, 'The release bench is not recorded')
        self.assertNotContains(response, 'data-progress-chart="release"')
        self.assertContains(response, 'Lineage: chained step estimates')
        self.assertLess(response.content.index(b'id="progress-release"'), response.content.index(b'Chained Elo'))

    def test_the_action_is_only_for_users_who_can_create_tests(self):
        self.release()
        self.login('disabled')
        response = self.client.get('/progress/Avalanche/')
        self.assertContains(response, 'No test of master against v4.0.0 yet.')
        self.assertNotContains(response, '/test/new/?release=')

    def test_anchor_runs_show_the_headline_the_chart_and_the_branch_list(self):
        self.release()
        merged, feature = 'c' * 40, 'f' * 40
        first = self.measured(merged)
        self.measured(feature)
        store.record_standing(
            'Avalanche', BranchStanding(merged, True, datetime(2026, 9, 1, tzinfo=UTC)), timezone.now()
        )
        self.login()

        response = self.client.get('/progress/Avalanche/?window=all')
        self.assertContains(response, 'STC Elo vs release')
        self.assertContains(response, 'data-progress-chart="release"')
        self.assertContains(response, 'id="progress-release-table"')
        self.assertContains(response, f'href="/test/{first.id}/"')
        self.assertContains(response, 'committed Sep 1, 2026')
        self.assertContains(response, 'Branch vs release: 1 measured against v4.0.0')
        self.assertNotContains(response, 'No test of master against')
        release = response.context['page'].release
        self.assertEqual([line.commit.label for line in release.lines], ['cccccccc'])
        self.assertEqual([line.commit.label for line in release.branches], ['ffffffff'])

    def test_an_unknown_release_offers_no_action(self):
        self.login()
        response = self.client.get('/progress/Avalanche/')
        self.assertContains(response, 'Since the latest release')
        self.assertContains(response, 'has not been looked up yet')
        self.assertNotContains(response, '/test/new/?release=')

    def test_a_failed_lookup_is_shown_with_the_known_release(self):
        self.release()
        store.unpin_release('Avalanche')
        store.record_failure('Avalanche', 'GitHub rate limit reached', timezone.now())
        self.login()
        response = self.client.get('/progress/Avalanche/')
        self.assertContains(response, 'Since v4.0.0')
        self.assertContains(response, 'class="progress-release-stale"')
        self.assertContains(response, 'GitHub lookup failing (GitHub rate limit reached)')

    def test_without_one_engine_there_is_no_release_section(self):
        EngineConfig.objects.create(name='Other', source=SOURCE)
        create_test(self.creator, finished=True, passed=True)
        create_test(self.creator, engine='Other', finished=True, passed=True)
        self.login()
        response = self.client.get('/progress/')
        self.assertIsNone(response.context['page'].release)
        self.assertNotContains(response, 'id="progress-release"')

    def test_the_api_carries_the_release(self):
        self.release()
        merged = 'c' * 40
        test = self.measured(merged)
        self.measured('f' * 40)
        store.record_standing('Avalanche', BranchStanding(merged, True, None), timezone.now())

        payload = self.client.post('/api/progress/?engine=Avalanche&window=all', credentials(self.creator)).json()
        release = payload['progress']['release']
        self.assertEqual(
            set(release),
            {
                'engine',
                'repo',
                'anchor',
                'series',
                'branches',
                'sprt',
                'points_omitted',
                'branches_omitted',
                'sprt_omitted',
            },
        )
        self.assertEqual(
            set(release['anchor']),
            {
                'engine',
                'tag',
                'sha',
                'published_at',
                'default_branch',
                'pinned',
                'fetched_at',
                'attempted_at',
                'error',
                'bench',
                'network',
            },
        )
        self.assertEqual((release['anchor']['tag'], release['anchor']['sha']), ('v4.0.0', RELEASE))
        (series,) = release['series']
        self.assertEqual(set(series), {'time_class', 'latest', 'points'})
        self.assertEqual(series['latest'], series['points'][0])
        point = series['latest']
        self.assertEqual(
            set(point),
            {
                'time_class',
                'base',
                'dev',
                'repo',
                'subject',
                'committed_at',
                'measured_at',
                'first_run',
                'newer_bases',
                'measurement',
            },
        )
        self.assertEqual((point['dev']['sha'], point['first_run'], point['time_class']), (merged, test.id, 'stc'))
        self.assertEqual(point['measurement']['runs'][0]['mode'], 'GAMES')
        self.assertEqual([branch['dev']['sha'] for branch in release['branches']], ['f' * 40])
        self.assertNotIn('measurements', payload['progress'])

    def test_the_report_asks_a_fixed_number_of_queries(self):
        self.release()
        with self.assertNumQueries(10):
            progress_report(Window.DAYS_90, 'Avalanche')
        for index in range(12):
            sha = f'{index:040x}'
            self.measured(sha)
            store.record_standing('Avalanche', BranchStanding(sha, index % 2 == 0, None), timezone.now())
        with self.assertNumQueries(10):
            progress_report(Window.DAYS_90, 'Avalanche')
        with self.assertNumQueries(9):
            report = progress_report(Window.ALL, 'Avalanche')
        self.assertEqual(len(present(report.release).series[0].points), 6)
        self.assertEqual(len(present(report.release).branches), 6)
