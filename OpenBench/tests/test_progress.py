import re
from datetime import UTC, date, datetime, timedelta
from unittest import mock

from django.core.cache import cache
from django.db import connection
from django.db.models.functions import TruncDate
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from OpenBench.insights.domain import Outcomes
from OpenBench.insights.strength import EloInterval
from OpenBench.models import Machine, Result, Test, WorkloadSnapshot
from OpenBench.progress import analysis, sources
from OpenBench.progress.domain import (
    DEFAULT_WINDOW,
    DailyGames,
    DayMaximum,
    GreenRow,
    OutcomeCounts,
    Window,
)
from OpenBench.progress.present import elo_text, progress_url, summary_tiles
from OpenBench.progress.report import progress_report
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


def green_row(id: int, finished_at: datetime, penta=PENTA) -> GreenRow:
    return GreenRow(
        id=id,
        name=f'patch-{id}',
        finished_at=finished_at,
        games=2 * sum(penta),
        elo_bounds=(0.0, 3.0),
        outcomes=Outcomes((0, 0, 0), penta, True),
    )


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
    def test_greens_accumulate_their_point_estimates_in_finish_order(self):
        late, early = NOW, NOW - timedelta(days=3)
        greens = analysis.green_tests([green_row(2, late, STRONG), green_row(1, early)])

        self.assertEqual([green.id for green in greens], [1, 2])
        expected = [Elo(PENTA)[1], Elo(PENTA)[1] + Elo(STRONG)[1]]
        for green, total in zip(greens, expected, strict=True):
            self.assertAlmostEqual(green.cumulative_elo, total)
        self.assertAlmostEqual(present(greens[0].elo).lower, Elo(PENTA)[0])

    def test_greens_without_an_estimate_add_nothing(self):
        greens = analysis.green_tests([green_row(1, NOW, (0, 0, 1, 0, 0)), green_row(2, NOW, PENTA)])
        self.assertIsNone(greens[0].elo)
        self.assertEqual(greens[0].cumulative_elo, 0.0)
        self.assertAlmostEqual(greens[1].cumulative_elo, Elo(PENTA)[1])

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

    def test_elo_steps_thin_to_the_limit_and_keep_both_ends(self):
        greens = analysis.green_tests([green_row(index, NOW + timedelta(hours=index)) for index in range(1200)])
        steps = analysis.elo_steps(greens, limit=500)
        self.assertEqual(len(steps), 500)
        self.assertEqual((steps[0].greens, steps[-1].greens), (1, 1200))
        self.assertEqual(steps[-1].cumulative_elo, greens[-1].cumulative_elo)
        for step in steps:
            self.assertEqual(step.cumulative_elo, greens[step.greens - 1].cumulative_elo)
        self.assertEqual(len(analysis.elo_steps(greens[:3], limit=500)), 3)

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
        greens = analysis.green_tests([green_row(1, NOW), green_row(2, NOW, STRONG)])
        daily = [DailyGames(date(2026, 9, day), 10 * day) for day in (1, 2, 3)]
        summary = analysis.summarize(
            greens,
            [OutcomeCounts(2, 3, 1), OutcomeCounts(1, 0, 0)],
            daily,
            {'a': 3, 'b': 1},
            {'w': 5, 'y': 2},
        )
        self.assertAlmostEqual(summary.elo_gained, greens[-1].cumulative_elo)
        self.assertEqual(summary.sprt, OutcomeCounts(3, 3, 1))
        self.assertEqual(summary.sprt_pass_rate, 0.5)
        self.assertEqual((summary.games, summary.games_per_day), (60, 20.0))
        self.assertEqual((summary.tests_created, summary.authors), (4, 2))
        self.assertEqual(summary.contributors, 2)

    def test_empty_summary(self):
        summary = analysis.summarize([], [], [], {}, {})
        self.assertEqual(summary.elo_gained, 0.0)
        self.assertIsNone(summary.sprt_pass_rate)
        self.assertIsNone(summary.games_per_day)


class PresentTests(SimpleTestCase):
    def test_elo_text(self):
        self.assertEqual(elo_text(EloInterval(-1.0, 2.5, 7.0)), '+2.50 ± 4.50')
        self.assertEqual(elo_text(EloInterval(-5.0, -2.0, 0.5)), '−2.00 ± 3.00')
        self.assertEqual(elo_text(None), '—')

    def test_urls_quote_the_engine(self):
        self.assertEqual(progress_url(None, Window.ALL), '/progress/?window=all')
        self.assertEqual(progress_url('A B', Window.DAYS_30), '/progress/A%20B/?window=30d')
        self.assertEqual(progress_url('A/B', Window.YEAR), '/progress/?engine=A%2FB&window=1y')

    def test_tiles_label_the_elo_sum_an_estimate(self):
        greens = analysis.green_tests([green_row(1, NOW)])
        tiles = summary_tiles(analysis.summarize(greens, [], [], {}, {}))
        self.assertEqual(tiles[0].label, 'Elo gained (estimate)')
        self.assertEqual(tiles[0].meta, 'sum over 1 green')


class ProgressDataTests(TestCase):
    def setUp(self):
        ensure_book()
        self.author = create_user('author')
        self.other = create_user('other')
        self.worker = create_user('worker')
        self.machine = Machine.objects.create(user=self.worker, info=system_info(), mnps=1.0)

    def sprt(self, finished_at, penta=PENTA, author=None, engine='Avalanche', **flags):
        wins, losses = 2 * penta[4] + penta[3], 2 * penta[0] + penta[1]
        test = create_test(
            author or self.author,
            engine=engine,
            games=2 * sum(penta),
            wins=wins,
            losses=losses,
            draws=2 * sum(penta) - wins - losses,
            LL=penta[0],
            LD=penta[1],
            DD=penta[2],
            DW=penta[3],
            WW=penta[4],
            finished=True,
            **flags,
        )
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

    def test_greens_follow_the_greens_definition(self):
        self.seed()
        report = self.report()
        self.assertEqual(
            [green.id for green in report.greens],
            [self.green.id, self.foreign.id, self.strong.id],
        )
        self.assertAlmostEqual(
            report.summary.elo_gained,
            2 * Elo(PENTA)[1] + Elo(STRONG)[1],
        )

    def test_finish_time_is_the_last_report_for_decided_tests(self):
        self.seed()
        Test.objects.filter(id=self.green.id).update(updated=NOW)
        report = self.report()
        green = next(green for green in report.greens if green.id == self.green.id)
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
        self.assertEqual([green.id for green in report.greens], [self.foreign.id])
        self.assertEqual(report.summary.games, 50)
        self.assertEqual(report.top_contributors, [])
        self.assertEqual(self.report(engine='Missing').summary.sprt.total, 0)

    def test_trinomial_greens_use_their_trinomial_counts(self):
        tri = create_test(
            self.author,
            games=300,
            losses=80,
            draws=100,
            wins=120,
            use_tri=True,
            use_penta=False,
            passed=True,
            finished=True,
        )
        Test.objects.filter(id=tri.id).update(updated=NOW - timedelta(days=1))
        (green,) = self.report().greens
        self.assertEqual(green.id, tri.id)
        self.assertAlmostEqual(green.elo.value, Elo((80, 100, 120))[1])
        self.assertAlmostEqual(green.cumulative_elo, Elo((80, 100, 120))[1])

    def test_greens_sent_are_capped_but_counted(self):
        self.seed()
        with mock.patch('OpenBench.progress.report.GREENS_SENT', 1):
            report = self.report()
        self.assertEqual([green.id for green in report.greens], [self.strong.id])
        self.assertEqual(report.greens_omitted, 2)
        self.assertEqual(report.summary.greens, 3)
        self.assertEqual(report.elo_steps[-1].greens, 3)

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
            test = self.sprt(NOW - timedelta(days=15, hours=index), passed=True)
            self.history(test, (NOW - timedelta(days=16), 10), (NOW - timedelta(days=15), 90))
            self.result(test, 90, NOW - timedelta(days=15))

        with self.assertNumQueries(6):
            large = self.report()
        self.assertEqual(len(large.greens), len(small.greens) + 25)
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
        self.assertEqual([green.url for green in page.greens], [f'/test/{self.test.id}/'])
        self.assertContains(response, 'id="progress-data"')
        self.assertContains(response, 'Elo gained (estimate)')
        self.assertContains(response, 'href="/progress/?window=30d"')

    def test_engine_path_and_query(self):
        self.login()
        response = self.client.get('/progress/Avalanche/?window=1y')
        self.assertEqual(response.context['page'].engine, 'Avalanche')
        self.assertEqual(response.context['page'].window, Window.YEAR)
        self.assertEqual(len(response.context['page'].greens), 1)
        self.assertEqual(self.client.get('/progress/Other/').context['page'].greens, [])

        redirect = self.client.get('/progress/?engine=Avalanche&window=30d')
        self.assertEqual(redirect.status_code, 302)
        self.assertEqual(redirect['Location'], '/progress/Avalanche/?window=30d')
        self.assertEqual(self.client.get('/progress/?engine=&window=all').status_code, 200)

        blank = self.client.get('/progress/%20/?window=1y')
        self.assertEqual(blank.status_code, 302)
        self.assertEqual(blank['Location'], '/progress/?window=1y')

        slashed = self.client.get('/progress/?engine=A/B')
        self.assertEqual(slashed.status_code, 200)
        self.assertEqual(slashed.context['page'].engine, 'A/B')
        self.assertContains(slashed, 'href="/progress/?engine=A%2FB&amp;window=30d"')

    def test_reports_are_cached_briefly(self):
        self.login()
        self.client.get('/progress/')
        Test.objects.filter(id=self.test.id).update(deleted=True)
        self.assertEqual(len(self.client.get('/progress/').context['page'].greens), 1)
        cache.clear()
        self.assertEqual(self.client.get('/progress/').context['page'].greens, [])

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
        self.assertEqual(len(payload['greens']), 1)

        other = self.client.post('/api/progress/?engine=Other', credentials(self.reader))
        self.assertEqual(other.json()['progress']['greens'], [])

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
                'elo_steps',
                'greens',
                'greens_omitted',
                'weekly_outcomes',
                'daily_games',
                'top_contributors',
                'top_authors',
            },
        )
        self.assertEqual(payload['window'], '90d')
        self.assertIsNone(payload['engine'])
        self.assertEqual(len(payload['daily_games']), 90)
        self.assertRegex(payload['start'], r'^\d{4}-\d{2}-\d{2}$')
        green = payload['greens'][0]
        self.assertEqual(
            set(green),
            {
                'id',
                'name',
                'finished_at',
                'games',
                'elo_bounds',
                'elo',
                'cumulative_elo',
            },
        )
        self.assertEqual(set(green['elo']), {'lower', 'value', 'upper'})
        self.assertEqual(payload['greens_omitted'], 0)
        self.assertEqual(
            payload['elo_steps'],
            [
                {
                    'finished_at': green['finished_at'],
                    'cumulative_elo': green['cumulative_elo'],
                    'greens': 1,
                }
            ],
        )
        self.assertEqual(set(payload['summary']['sprt']), {'passed', 'failed', 'stopped'})
        self.assertEqual(
            set(payload['weekly_outcomes'][0]),
            {'week_start', 'passed', 'failed', 'stopped'},
        )
        self.assertTrue(re.match(r'^\d{4}-\d{2}-\d{2}T', green['finished_at']))

    def test_api_rejects_an_unknown_window(self):
        self.login()
        response = self.client.get('/api/progress/?window=decade')
        self.assertEqual(response.status_code, 400)
        self.assertIn('30d', response.json()['error'])
