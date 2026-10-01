import re
from datetime import UTC, date, datetime, timedelta
from unittest import mock

from django.core.cache import cache
from django.db import connection
from django.db.models.functions import TruncDate
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from OpenBench.insights.strength import EloInterval
from OpenBench.models import Engine, Machine, Result, Test, WorkloadSnapshot
from OpenBench.progress import analysis, sources
from OpenBench.progress.conditions import time_class
from OpenBench.progress.domain import (
    DEFAULT_WINDOW,
    NO_LINEAGE,
    DailyGames,
    DayMaximum,
    LineageSummary,
    OutcomeCounts,
    RunStatus,
    TimeClass,
    Window,
)
from OpenBench.progress.present import elo_text, progress_url, summary_tiles
from OpenBench.progress.report import progress_report
from OpenBench.progress.views import report_cache_key
from OpenBench.stats import Elo
from OpenBench.tests.fixtures import (
    PASSWORD,
    create_engine_config,
    create_test,
    create_user,
    credentials,
    ensure_book,
    present,
    system_info,
)

NOW = datetime(2026, 9, 30, 12, tzinfo=UTC)
PENTA = (5, 40, 100, 45, 10)
STRONG = (2, 20, 100, 60, 18)


class WindowParsingTests(SimpleTestCase):
    def test_missing_or_blank_is_the_default(self):
        self.assertEqual(analysis.parse_window(None), DEFAULT_WINDOW)
        self.assertEqual(analysis.parse_window(''), Window.DAYS_90)

    def test_known_values_ignore_case_and_space(self):
        self.assertEqual(analysis.parse_window('30d'), Window.DAYS_30)
        self.assertEqual(analysis.parse_window(' 1Y '), Window.YEAR)
        self.assertEqual(analysis.parse_window('ALL'), Window.ALL)

    def test_unknown_values_are_rejected(self):
        for raw in ('7d', '90', 'forever', '1y; drop'):
            self.assertIsNone(analysis.parse_window(raw), raw)

    def test_engine_names_are_trimmed_and_bounded(self):
        self.assertIsNone(analysis.parse_engine(None))
        self.assertIsNone(analysis.parse_engine('   '))
        self.assertEqual(analysis.parse_engine(' Avalanche '), 'Avalanche')
        self.assertEqual(len(present(analysis.parse_engine('x' * 500))), 64)

    def test_scopes_start_at_utc_midnight(self):
        scope = analysis.make_scope(Window.DAYS_30, None, NOW)
        self.assertEqual(scope.since, datetime(2026, 9, 1, tzinfo=UTC))
        self.assertEqual(scope.today, date(2026, 9, 30))
        self.assertEqual(
            analysis.make_scope(Window.YEAR, None, NOW).since,
            datetime(2025, 10, 1, tzinfo=UTC),
        )
        self.assertIsNone(analysis.make_scope(Window.ALL, 'X', NOW).since)


class AnalysisTests(SimpleTestCase):
    def test_games_by_day_differences_daily_maxima_from_the_baseline(self):
        first, second = date(2026, 9, 1), date(2026, 9, 2)
        maxima = [
            DayMaximum(1, second, 500),
            DayMaximum(1, first, 200),
            DayMaximum(2, first, 80),
            DayMaximum(3, second, 40),
        ]
        totals = analysis.games_by_day(maxima, {1: 150, 3: 60})
        self.assertEqual(totals, {first: 50 + 80, second: 300})

    def test_series_fill_every_day_and_week(self):
        start, end = date(2026, 9, 3), date(2026, 9, 16)
        daily = analysis.daily_series({date(2026, 9, 5): 7}, start, end)
        self.assertEqual(len(daily), 14)
        self.assertEqual(daily[2], DailyGames(date(2026, 9, 5), 7))
        self.assertEqual(sum(day.games for day in daily), 7)

        weeks = analysis.weekly_series({date(2026, 9, 7): OutcomeCounts(2, 1, 0)}, start, end)
        self.assertEqual(
            [week.week_start for week in weeks],
            [date(2026, 8, 31), date(2026, 9, 7), date(2026, 9, 14)],
        )
        self.assertEqual((weeks[1].passed, weeks[1].failed), (2, 1))

    def test_week_start_is_monday(self):
        self.assertEqual(analysis.week_start(date(2026, 9, 30)), date(2026, 9, 28))
        self.assertEqual(analysis.week_start(date(2026, 9, 28)), date(2026, 9, 28))

    def test_all_time_starts_at_the_earliest_data(self):
        scope = analysis.make_scope(Window.ALL, None, NOW)
        self.assertEqual(
            analysis.series_start(scope, [None, date(2026, 5, 2), date(2026, 4, 9)]),
            date(2026, 4, 9),
        )
        self.assertEqual(analysis.series_start(scope, [None]), date(2026, 9, 30))

    def test_rankings_are_bounded_and_carry_shares(self):
        games = {f'user-{index:02}': 100 - index for index in range(15)}
        top = analysis.top_contributors(games)
        self.assertEqual(len(top), 10)
        self.assertEqual(top[0].username, 'user-00')
        self.assertAlmostEqual(present(top[0].share), 100 / sum(games.values()))

        authors = analysis.top_authors({'b': 2, 'a': 2, 'c': 5})
        self.assertEqual([author.username for author in authors], ['c', 'a', 'b'])

    def test_summary(self):
        lineage = LineageSummary(steps_accepted=3, candidates=1, measurements=5, runs=6)
        daily = [DailyGames(date(2026, 9, day), 10 * day) for day in (1, 2, 3)]
        summary = analysis.summarize(
            lineage,
            [OutcomeCounts(2, 3, 1), OutcomeCounts(1, 0, 0)],
            daily,
            {'a': 3, 'b': 1},
            {'w': 5, 'y': 2},
        )
        self.assertEqual(summary.lineage, lineage)
        self.assertEqual(summary.sprt, OutcomeCounts(3, 3, 1))
        self.assertEqual(summary.sprt_pass_rate, 0.5)
        self.assertEqual((summary.games, summary.games_per_day), (60, 20.0))
        self.assertEqual((summary.tests_created, summary.authors), (4, 2))
        self.assertEqual(summary.contributors, 2)

    def test_empty_summary(self):
        summary = analysis.summarize(NO_LINEAGE, [], [], {}, {})
        self.assertEqual(summary.lineage.steps_accepted, 0)
        self.assertIsNone(summary.sprt_pass_rate)
        self.assertIsNone(summary.games_per_day)


class PresentTests(SimpleTestCase):
    def test_elo_text(self):
        self.assertEqual(elo_text(EloInterval(-1.0, 2.5, 7.0)), '+2.50 ± 4.00')
        self.assertEqual(elo_text(EloInterval(-5.0, -2.0, 0.5)), '−2.00 ± 2.75')
        self.assertEqual(elo_text(None), '—')

    def test_urls_quote_the_engine(self):
        self.assertEqual(progress_url(None, Window.ALL), '/progress/?window=all')
        self.assertEqual(progress_url('A B', Window.DAYS_30), '/progress/A%20B/?window=30d')
        self.assertEqual(progress_url('A/B', Window.YEAR), '/progress/?engine=A%2FB&window=1y')

    def test_tiles_never_headline_a_sum_across_classes(self):
        tiles = summary_tiles(analysis.summarize(NO_LINEAGE, [], [], {}, {}))
        self.assertEqual([tile.label for tile in tiles[:2]], ['Chained Elo · STC', 'Chained Elo · LTC'])
        self.assertEqual(tiles[0].value, '—')
        self.assertEqual(tiles[0].meta, 'no trunk step measured at STC')
        self.assertNotIn('Elo gained', ' '.join(tile.label for tile in tiles))


class ProgressDataTests(TestCase):
    def setUp(self):
        ensure_book()
        self.author = create_user('author')
        self.other = create_user('other')
        self.worker = create_user('worker')
        self.machine = Machine.objects.create(user=self.worker, info=system_info(), mnps=1.0)

    def pinned(self, base, dev, finished_at=NOW, penta=PENTA, **fields):
        test = self.sprt(finished_at, penta, **{'passed': True, **fields})
        Engine.objects.filter(id=test.dev_id).update(sha=(dev * 40)[:40])
        Engine.objects.filter(id=test.base_id).update(sha=(base * 40)[:40])
        return test

    def sprt(self, finished_at, penta=PENTA, author=None, engine='Avalanche', **flags):
        wins, losses = 2 * penta[4] + penta[3], 2 * penta[0] + penta[1]
        counts = {
            'games': 2 * sum(penta),
            'wins': wins,
            'losses': losses,
            'draws': 2 * sum(penta) - wins - losses,
            'LL': penta[0],
            'LD': penta[1],
            'DD': penta[2],
            'DW': penta[3],
            'WW': penta[4],
            'finished': True,
        }
        test = create_test(author or self.author, engine=engine, **{**counts, **flags})
        Test.objects.filter(id=test.id).update(creation=finished_at - timedelta(hours=6), updated=finished_at)
        return test

    def history(self, test, *points):
        WorkloadSnapshot.objects.bulk_create(
            WorkloadSnapshot(test=test, created=created, games=games) for created, games in points
        )

    def result(self, test, games, updated):
        result = Result.objects.create(test=test, machine=self.machine, games=games, wins=games // 2)
        Result.objects.filter(id=result.id).update(updated=updated)

    def seed(self, extra_greens=0):
        self.green = self.sprt(NOW - timedelta(days=10), passed=True)
        self.strong = self.sprt(NOW - timedelta(days=2), STRONG, passed=True)
        self.blue = self.sprt(NOW - timedelta(days=3), passed=True, elolower=-3.0, eloupper=0.0)
        self.red = self.sprt(NOW - timedelta(days=3), failed=True, author=self.other)
        self.stopped = self.sprt(NOW - timedelta(days=4))
        self.deleted = self.sprt(NOW - timedelta(days=5), passed=True, deleted=True)
        self.old = self.sprt(NOW - timedelta(days=200), passed=True)
        self.foreign = self.sprt(NOW - timedelta(days=6), passed=True, engine='Other')
        self.sprt(NOW - timedelta(days=1), test_mode='SPSA')
        for index in range(extra_greens):
            self.sprt(NOW - timedelta(days=20, hours=index), passed=True)

        self.history(
            self.green,
            (NOW - timedelta(days=100), 0),
            (NOW - timedelta(days=12), 100),
            (NOW - timedelta(days=11, hours=1), 300),
            (NOW - timedelta(days=10), 380),
        )
        self.history(self.foreign, (NOW - timedelta(days=6), 50))
        self.result(self.green, 380, NOW - timedelta(days=10))
        self.result(self.old, 999, NOW - timedelta(days=200))

    def report(self, window=Window.DAYS_90, engine=None):
        return progress_report(window, engine, now=NOW)

    def test_lineage_chains_pinned_commits_and_pools_repeats(self):
        first = self.pinned('1', '2', NOW - timedelta(days=9))
        repeat = self.pinned('1', '2', NOW - timedelta(days=8), STRONG)
        ltc = self.pinned(
            '1', '2', NOW - timedelta(days=7), dev_time_control='40.0+0.40', base_time_control='40.0+0.40'
        )
        second = self.pinned('2', '3', NOW - timedelta(days=5))
        failed = self.pinned('2', '4', NOW - timedelta(days=4), passed=False, failed=True)
        self.pinned('3', '3', NOW - timedelta(days=3), test_mode='GAMES')
        self.pinned('5', '6', NOW - timedelta(days=2), deleted=True)
        self.pinned('5', '6', NOW - timedelta(days=2), test_mode='SPSA')

        lineage = present(self.report(engine='Avalanche').lineage)
        self.assertEqual([row.step.first_run for row in lineage.steps], [first.id, second.id])
        self.assertEqual(lineage.classes, [TimeClass.STC, TimeClass.LTC])
        self.assertEqual(lineage.detached, [])

        stc = present(lineage.steps[0].step.measurement(TimeClass.STC))
        self.assertEqual([run.id for run in stc.runs], [first.id, repeat.id])
        pooled = tuple(a + b for a, b in zip(PENTA, STRONG, strict=True))
        self.assertAlmostEqual(present(stc.elo).value, Elo(pooled)[1])
        self.assertEqual(present(lineage.steps[0].step.measurement(TimeClass.LTC)).runs[0].id, ltc.id)
        self.assertEqual([found.step.first_run for found in lineage.steps[0].candidates], [failed.id])

        series = {found.time_class: found for found in lineage.series}
        self.assertAlmostEqual(present(series[TimeClass.STC].total).value, Elo(pooled)[1] + Elo(PENTA)[1])
        self.assertEqual((series[TimeClass.LTC].measured, series[TimeClass.LTC].steps), (1, 2))
        self.assertIsNone(series[TimeClass.LTC].points[1].cumulative)

    def test_run_statuses_and_finish_times(self):
        running = self.pinned('1', '2', NOW, passed=False, finished=False)
        games = self.pinned('1', '2', NOW - timedelta(days=1), test_mode='GAMES')
        pending = self.pinned('1', '3', NOW, passed=False, finished=False, approved=False)
        self.history(games, (NOW - timedelta(days=2), 100))
        rows = {row.id: row for row in sources.load_runs('Avalanche', time_class)}
        self.assertEqual(rows[running.id].status, RunStatus.RUNNING)
        self.assertIsNone(rows[running.id].finished_at)
        self.assertEqual(rows[games.id].status, RunStatus.COMPLETED)
        self.assertEqual(rows[games.id].finished_at, NOW - timedelta(days=2))
        self.assertEqual(rows[pending.id].status, RunStatus.PENDING)

    def test_the_window_keeps_a_suffix_of_the_trunk(self):
        self.pinned('1', '2', NOW - timedelta(days=200))
        recent = self.pinned('2', '3', NOW - timedelta(days=5))
        report = self.report(Window.DAYS_30, 'Avalanche')
        lineage = present(report.lineage)
        self.assertEqual([(row.index, row.step.first_run) for row in lineage.steps], [(2, recent.id)])
        self.assertEqual((lineage.trunk_length, lineage.origin.sha), (2, '2' * 40))
        self.assertEqual(report.summary.lineage.steps_accepted, 1)
        self.assertEqual(len(present(self.report(Window.ALL, 'Avalanche').lineage).steps), 2)

    def test_all_engines_show_a_lineage_only_when_one_engine_has_steps(self):
        self.pinned('1', '2')
        self.assertEqual(present(self.report().lineage).engine, 'Avalanche')
        self.pinned('7', '8', engine='Other')
        self.pinned('1', '9', base_engine='Other')
        report = self.report()
        self.assertIsNone(report.lineage)
        self.assertEqual(report.lineage_engines, ['Avalanche', 'Other'])
        self.assertEqual(report.summary.lineage, NO_LINEAGE)
        self.assertEqual(len(present(self.report(engine='Other').lineage).steps), 1)

    def test_finish_time_is_the_last_report_for_decided_tests(self):
        self.seed()
        Test.objects.filter(id=self.green.id).update(updated=NOW)
        green = Test.objects.annotate(finished_at=sources.finish_time()).get(id=self.green.id)
        self.assertEqual(green.finished_at, NOW - timedelta(days=10))

    def test_finish_time_of_a_stopped_test_survives_later_edits(self):
        self.seed()
        self.history(self.stopped, (NOW - timedelta(days=40), 0), (NOW - timedelta(days=35), 200))
        Test.objects.filter(id=self.stopped.id).update(updated=NOW)
        stopped = Test.objects.annotate(finished_at=sources.finish_time()).get(id=self.stopped.id)
        self.assertEqual(stopped.finished_at, NOW - timedelta(days=35))

    def test_weekly_outcomes_count_sprt_only(self):
        self.seed()
        report = self.report(Window.DAYS_30)
        self.assertEqual(report.summary.sprt, OutcomeCounts(passed=4, failed=1, stopped=1))
        self.assertEqual(report.summary.sprt_pass_rate, 4 / 5)
        self.assertEqual(report.weekly_outcomes[-1].week_start, date(2026, 9, 28))
        self.assertEqual(report.weekly_outcomes[0].week_start, date(2026, 8, 31))
        self.assertEqual(len(report.weekly_outcomes), 5)

    def test_games_per_day_use_the_baseline_before_the_window(self):
        self.seed()
        days = {day.day: day.games for day in self.report(Window.DAYS_30).daily_games}
        self.assertEqual(days[date(2026, 9, 18)], 100)
        self.assertEqual(days[date(2026, 9, 19)], 200)
        self.assertEqual(days[date(2026, 9, 20)], 80)
        self.assertEqual(days[date(2026, 9, 24)], 50)
        self.assertEqual(sum(days.values()), 430)

        everything = self.report(Window.ALL)
        self.assertEqual(everything.start, date(2026, 3, 9))
        self.assertEqual(everything.summary.games, 430)

    def test_contributors_and_authors_in_the_window(self):
        self.seed()
        report = self.report()
        self.assertEqual(
            [(row.username, row.games) for row in report.top_contributors],
            [('worker', 380)],
        )
        self.assertEqual(
            [(row.username, row.tests) for row in report.top_authors],
            [('author', 6), ('other', 1)],
        )
        self.assertEqual(self.report(Window.ALL).top_contributors[0].games, 1379)

    def test_engine_filter(self):
        self.seed()
        report = self.report(engine='Other')
        self.assertEqual(report.summary.games, 50)
        self.assertEqual(report.top_contributors, [])
        self.assertEqual(self.report(engine='Missing').summary.sprt.total, 0)
        self.assertIsNone(self.report(engine='Missing').lineage)

    def test_trinomial_runs_pool_their_trinomial_counts(self):
        tri = {'use_tri': True, 'use_penta': False}
        self.pinned('1', '2', NOW - timedelta(days=2), games=300, losses=80, draws=100, wins=120, **tri)
        self.pinned('1', '2', NOW - timedelta(days=1))
        step = present(self.report(engine='Avalanche').lineage).steps[0].step
        wins, losses = 2 * PENTA[4] + PENTA[3], 2 * PENTA[0] + PENTA[1]
        pooled = (80 + losses, 100 + 2 * sum(PENTA) - wins - losses, 120 + wins)
        measurement = present(step.measurement(TimeClass.STC))
        self.assertEqual(measurement.pooling.value, 'trinomial')
        self.assertAlmostEqual(present(measurement.elo).value, Elo(pooled)[1])

    def test_utc_days_match_truncdate_around_midnight(self):
        test = self.sprt(NOW)
        midnight = datetime(2026, 9, 20, tzinfo=UTC)
        moments = [
            midnight - timedelta(microseconds=1),
            midnight,
            midnight + timedelta(microseconds=1),
            midnight + timedelta(hours=23, minutes=59, seconds=59),
        ]
        self.history(test, *((moment, index) for index, moment in enumerate(moments)))
        snapshots = WorkloadSnapshot.objects.filter(test=test).order_by('created')
        built_in = list(snapshots.annotate(day=sources.utc_date('created')).values_list('day', flat=True))
        truncated = list(snapshots.annotate(day=TruncDate('created', tzinfo=UTC)).values_list('day', flat=True))
        self.assertEqual(built_in, truncated)
        self.assertEqual(built_in, [date(2026, 9, 19), *[date(2026, 9, 20)] * 3])
        if connection.vendor == 'sqlite':
            self.assertNotIsInstance(sources.utc_date('created'), TruncDate)

    def test_query_count_does_not_grow_with_the_data(self):
        self.seed()
        with self.assertNumQueries(6):
            small = self.report()

        for index in range(25):
            test = self.pinned(f'{index:02x}', f'{index + 1:02x}', NOW - timedelta(days=15, hours=-index))
            self.history(test, (NOW - timedelta(days=16), 10), (NOW - timedelta(days=15), 90))
            self.result(test, 90, NOW - timedelta(days=15))

        with self.assertNumQueries(6):
            self.report()
        self.assertIsNone(small.lineage)
        self.assertEqual(len(present(self.report(engine='Avalanche').lineage).detached), 25)
        with self.assertNumQueries(5):
            self.report(Window.ALL)


class ProgressViewTests(TestCase):
    def setUp(self):
        ensure_book()
        create_engine_config()
        self.reader = create_user('reader')
        self.disabled = create_user('disabled', enabled=False)
        now = timezone.now()
        self.test = create_test(
            self.reader,
            games=400,
            LL=5,
            LD=40,
            DD=100,
            DW=45,
            WW=10,
            passed=True,
            finished=True,
        )
        WorkloadSnapshot.objects.create(test=self.test, created=now - timedelta(days=2), games=400)
        Test.objects.filter(id=self.test.id).update(updated=now - timedelta(days=2))

        cache.clear()

    def login(self):
        self.client.post('/login/', {'username': 'reader', 'password': PASSWORD})

    def test_anonymous_is_redirected(self):
        for url in ('/progress/', '/progress/Avalanche/?window=30d'):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 302, url)
            self.assertEqual(response['Location'], '/login/')

    def test_page_renders_tiles_charts_and_data(self):
        self.login()
        response = self.client.get('/progress/')
        self.assertEqual(response.status_code, 200)
        page = response.context['page']
        self.assertEqual(page.window, Window.DAYS_90)
        self.assertEqual(page.lineage.lines[0].cells[0].runs[0].url, f'/test/{self.test.id}/')
        self.assertContains(response, 'id="progress-data"')
        self.assertContains(response, 'Chained Elo · STC')
        self.assertContains(response, 'https://github.com/SnowballSH/Avalanche/compare/' + 'b' * 40 + '...' + 'a' * 40)
        self.assertContains(response, 'href="/progress/?window=30d"')

    def test_engine_path_and_query(self):
        self.login()
        response = self.client.get('/progress/Avalanche/?window=1y')
        self.assertEqual(response.context['page'].engine, 'Avalanche')
        self.assertEqual(response.context['page'].window, Window.YEAR)
        self.assertEqual(len(response.context['page'].lineage.lines), 1)
        create_engine_config('Other')
        self.assertIsNone(self.client.get('/progress/Other/').context['page'].lineage)
        self.assertContains(self.client.get('/progress/Other/'), 'No test of one commit against another yet.')

        redirect = self.client.get('/progress/?engine=Avalanche&window=30d')
        self.assertEqual(redirect.status_code, 302)
        self.assertEqual(redirect['Location'], '/progress/Avalanche/?window=30d')
        self.assertEqual(self.client.get('/progress/?engine=&window=all').status_code, 200)

        blank = self.client.get('/progress/%20/?window=1y')
        self.assertEqual(blank.status_code, 302)
        self.assertEqual(blank['Location'], '/progress/?window=1y')

        create_engine_config('A/B')
        slashed = self.client.get('/progress/?engine=A/B')
        self.assertEqual(slashed.status_code, 200)
        self.assertEqual(slashed.context['page'].engine, 'A/B')
        self.assertContains(slashed, 'href="/progress/?engine=A%2FB&amp;window=30d"')

    def test_unknown_engines_are_refused_without_a_report(self):
        self.login()
        with mock.patch('OpenBench.progress.views.progress_report') as report:
            for url in ('/progress/Missing/', '/progress/?engine=Mis/sing'):
                self.assertEqual(self.client.get(url).status_code, 404, url)
            response = self.client.get('/api/progress/?engine=Missing')
            self.assertEqual(response.status_code, 404)
            self.assertIn('error', response.json())
            report.assert_not_called()
        for engine in ('Missing', 'Mis/sing'):
            self.assertIsNone(cache.get(report_cache_key(DEFAULT_WINDOW, engine)))

    def test_reports_are_cached_briefly(self):
        self.login()
        self.client.get('/progress/')
        Test.objects.filter(id=self.test.id).update(deleted=True)
        self.assertIsNotNone(self.client.get('/progress/').context['page'].lineage)
        cache.clear()
        self.assertIsNone(self.client.get('/progress/').context['page'].lineage)

    def test_unknown_window_falls_back_to_the_default(self):
        self.login()
        response = self.client.get('/progress/?window=decade')
        self.assertEqual(response.context['page'].window, DEFAULT_WINDOW)

    def test_page_query_count_is_bounded(self):
        self.login()
        first = self.page_queries()
        for _ in range(10):
            test = create_test(self.reader, passed=True, finished=True)
            WorkloadSnapshot.objects.create(test=test, games=20)
        self.assertEqual(self.page_queries(), first)

    def page_queries(self) -> int:
        cache.clear()
        with CaptureQueriesContext(connection) as queries:
            self.assertEqual(self.client.get('/progress/').status_code, 200)
        return len(queries)

    def test_api_requires_authentication(self):
        response = self.client.get('/api/progress/')
        self.assertEqual(response.status_code, 401)
        self.assertIn('error', response.json())

        disabled = self.client.post('/api/progress/', credentials(self.disabled))
        self.assertEqual(disabled.status_code, 401)

    def test_api_accepts_credentials_and_filters(self):
        payload = self.client.post('/api/progress/?window=all&engine=Avalanche', credentials(self.reader)).json()[
            'progress'
        ]
        self.assertEqual(payload['window'], 'all')
        self.assertEqual(payload['engine'], 'Avalanche')
        self.assertEqual(len(payload['lineage']['steps']), 1)

        create_engine_config('Other')
        other = self.client.post('/api/progress/?engine=Other', credentials(self.reader))
        self.assertIsNone(other.json()['progress']['lineage'])

    def test_api_shape(self):
        self.login()
        payload = self.client.get('/api/progress/').json()['progress']
        self.assertEqual(
            set(payload),
            {
                'generated_at',
                'engine',
                'window',
                'start',
                'end',
                'summary',
                'lineage',
                'lineage_engines',
                'weekly_outcomes',
                'daily_games',
                'top_contributors',
                'top_authors',
            },
        )
        self.assertEqual(payload['window'], '90d')
        self.assertIsNone(payload['engine'])
        self.assertEqual(payload['lineage_engines'], ['Avalanche'])
        self.assertEqual(len(payload['daily_games']), 90)
        self.assertRegex(payload['start'], r'^\d{4}-\d{2}-\d{2}$')
        self.assertEqual(
            payload['summary']['lineage'], {'steps_accepted': 1, 'candidates': 0, 'measurements': 1, 'runs': 1}
        )

        lineage = payload['lineage']
        self.assertEqual(
            set(lineage),
            {
                'engine',
                'classes',
                'origin',
                'origin_candidates',
                'origin_candidates_omitted',
                'head',
                'trunk_length',
                'steps',
                'steps_omitted',
                'series',
                'direct',
                'detached',
                'detached_omitted',
            },
        )
        self.assertEqual((lineage['engine'], lineage['classes']), ('Avalanche', ['stc']))
        self.assertEqual(lineage['origin'], {'sha': 'b' * 40, 'network': ''})
        self.assertEqual(lineage['head'], {'sha': 'a' * 40, 'network': ''})

        (row,) = lineage['steps']
        self.assertEqual(set(row), {'index', 'step', 'candidates', 'candidates_omitted'})
        step = row['step']
        self.assertEqual(
            set(step),
            {
                'base',
                'dev',
                'repo',
                'subject',
                'author',
                'first_run',
                'first_tested_at',
                'last_tested_at',
                'measured_at',
                'measurements',
            },
        )
        (measurement,) = step['measurements']
        self.assertEqual(set(measurement), {'time_class', 'verdict', 'games', 'pooling', 'elo', 'runs'})
        self.assertEqual((measurement['time_class'], measurement['verdict']), ('stc', 'passed'))
        self.assertEqual(set(measurement['elo']), {'lower', 'value', 'upper'})
        (run,) = measurement['runs']
        self.assertEqual(
            set(run), {'id', 'mode', 'status', 'time_control', 'created_at', 'finished_at', 'games', 'elo'}
        )
        self.assertTrue(re.match(r'^\d{4}-\d{2}-\d{2}T', run['finished_at']))

        (series,) = lineage['series']
        self.assertEqual(set(series), {'time_class', 'points', 'total', 'measured', 'steps'})
        self.assertEqual(series['points'], [{'index': 1, 'elo': measurement['elo'], 'cumulative': series['total']}])
        self.assertEqual(set(payload['summary']['sprt']), {'passed', 'failed', 'stopped'})
        self.assertEqual(
            set(payload['weekly_outcomes'][0]),
            {'week_start', 'passed', 'failed', 'stopped'},
        )

    def test_api_rejects_an_unknown_window(self):
        self.login()
        response = self.client.get('/api/progress/?window=decade')
        self.assertEqual(response.status_code, 400)
        self.assertIn('30d', response.json()['error'])
