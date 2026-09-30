import csv
import io
import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from OpenBench.compare.analysis import (
    difference_series,
    elo_difference,
    parse_query,
    parse_workload_id,
)
from OpenBench.insights.export import HISTORY_COLUMNS, cell_text, history_csv
from OpenBench.insights.series import SeriesPoint
from OpenBench.insights.strength import EloInterval
from OpenBench.models import SPSARun, Test, WorkloadSnapshot
from OpenBench.tests.fixtures import create_engine_config, create_test, create_user, credentials, ensure_book
from OpenBench.tests.test_csp import inline_code

PENTA = (5, 40, 100, 45, 10)
SAFE_CELL = re.compile(r'|-?\d+(\.\d+)?(e-?\d+)?|\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?\+00:00')


def point(games: int, elo: float | None, half: float = 2.0, llr: float | None = None) -> SeriesPoint:
    return SeriesPoint(
        timestamp=datetime(2026, 9, 30, tzinfo=UTC) + timedelta(minutes=games),
        games=games,
        llr=llr,
        elo=elo,
        elo_lower=None if elo is None else elo - half,
        elo_upper=None if elo is None else elo + half,
    )


def played(fraction: float = 1.0) -> dict[str, Any]:
    penta = [round(count * fraction) for count in PENTA]
    wins, losses = 2 * penta[4] + penta[3], 2 * penta[0] + penta[1]
    return {
        'games': 2 * sum(penta),
        'wins': wins,
        'losses': losses,
        'draws': 2 * sum(penta) - wins - losses,
        'LL': penta[0],
        'LD': penta[1],
        'DD': penta[2],
        'DW': penta[3],
        'WW': penta[4],
    }


def record_history(test: Test, points: int) -> None:
    now = timezone.now()
    WorkloadSnapshot.objects.bulk_create(
        [
            WorkloadSnapshot(
                test=test,
                created=now - timedelta(minutes=points - index),
                llr=0.1 * index,
                **played((index + 1) / points),
            )
            for index in range(points)
        ]
    )


class ParseQueryTests(SimpleTestCase):
    def test_workload_ids(self) -> None:
        self.assertEqual(parse_workload_id('42'), 42)
        for raw in ('', '0', '-1', '1.5', 'x', '1' * 19, ' 7'):
            with self.subTest(raw=raw):
                self.assertIsNone(parse_workload_id(raw))

    def test_blank_query_has_no_errors(self) -> None:
        query = parse_query({})
        self.assertTrue(query.blank)
        self.assertEqual(query.errors, ())
        self.assertIsNone(query.pair)

    def test_valid_pair(self) -> None:
        query = parse_query({'a': ' 3 ', 'b': '9'})
        self.assertEqual(query.pair, (3, 9))

    def test_errors(self) -> None:
        cases = {
            ('3', ''): ['Enter a workload id for B.'],
            ('x', '2'): ['Workload A must be a positive whole number.'],
            ('5', '5'): ['Choose two different workloads.'],
        }
        for (a, b), errors in cases.items():
            with self.subTest(a=a, b=b):
                query = parse_query({'a': a, 'b': b})
                self.assertEqual(list(query.errors), errors)
                self.assertIsNone(query.pair)


class DifferenceTests(SimpleTestCase):
    def test_final_difference_adds_half_widths_in_quadrature(self) -> None:
        difference = elo_difference(EloInterval(7.0, 10.0, 13.0), EloInterval(0.0, 4.0, 8.0))
        assert difference is not None
        self.assertAlmostEqual(difference.value, 6.0)
        self.assertAlmostEqual(difference.upper - difference.value, 5.0)
        self.assertAlmostEqual(difference.value - difference.lower, 5.0)
        self.assertIsNone(elo_difference(None, EloInterval(0.0, 1.0, 2.0)))

    def test_series_interpolates_on_the_overlapping_games(self) -> None:
        a = [point(0, None), point(100, 10.0, 4.0), point(300, 30.0, 2.0)]
        b = [point(200, 0.0, 3.0), point(400, 20.0, 3.0)]
        series = difference_series(a, b)

        self.assertEqual([item.games for item in series], [200, 300])
        self.assertAlmostEqual(series[0].value, 20.0)
        self.assertAlmostEqual(series[0].upper - series[0].value, (3.0**2 + 3.0**2) ** 0.5)
        self.assertAlmostEqual(series[1].value, 30.0 - 10.0)

    def test_disjoint_or_missing_histories_have_no_difference(self) -> None:
        self.assertEqual(difference_series([point(10, 1.0), point(20, 2.0)], [point(30, 1.0)]), [])
        self.assertEqual(difference_series([point(10, None)], [point(10, 1.0)]), [])


class HistoryCsvTests(SimpleTestCase):
    def test_rows_follow_the_columns(self) -> None:
        text = history_csv([point(64, 43.7, llr=0.089), point(128, None)])
        rows = list(csv.reader(io.StringIO(text)))
        self.assertEqual(tuple(rows[0]), HISTORY_COLUMNS)
        self.assertEqual(rows[1], ['2026-09-30T01:04:00+00:00', '64', '0.089', '43.7', '41.7', '45.7'])
        self.assertEqual(rows[2], ['2026-09-30T02:08:00+00:00', '128', '', '', '', ''])

    def test_only_numbers_and_timestamps_are_exported(self) -> None:
        for value in ('=1+1', '@SUM(A1)', True, b'1'):
            with self.subTest(value=value), self.assertRaises(TypeError):
                cell_text(value)
        self.assertEqual(cell_text(float('nan')), '')
        self.assertEqual(cell_text(-3.5), '-3.5')


class CompareViewTests(TestCase):
    def setUp(self) -> None:
        create_engine_config()
        ensure_book()
        self.user = create_user('reader')
        self.client.force_login(self.user)
        self.a = create_test(self.user, currentllr=1.2, **played())
        self.b = create_test(self.user, currentllr=-0.4, elolower=-3.0, eloupper=1.0, **played(0.6))
        record_history(self.a, 12)
        record_history(self.b, 8)

    def compare(self, a: object, b: object) -> str:
        return f'/compare/?a={a}&b={b}'

    def payload(self, html: str) -> dict[str, object]:
        match = re.search(r'<script id="compare-data" type="application/json">(.*?)</script>', html, re.S)
        assert match is not None
        data: dict[str, object] = json.loads(match.group(1))
        return data

    def test_two_sprt_workloads(self) -> None:
        response = self.client.get(self.compare(self.a.id, self.b.id))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()

        for text in ('Elo (95%)', 'Normalized Elo (95%)', 'LOS', 'LLR (bounds)', 'Time left', 'approximate'):
            self.assertIn(text, html)
        self.assertIn(f'href="/test/{self.a.id}/"', html)
        for chart in ('elo', 'difference', 'llr', 'games'):
            self.assertIn(f'data-compare-chart="{chart}"', html)

        data = self.payload(html)
        self.assertEqual((data['show_elo'], data['show_llr']), (True, True))
        workloads = data['workloads']
        assert isinstance(workloads, list)
        self.assertEqual([workload['key'] for workload in workloads], ['a', 'b'])
        self.assertEqual(len(workloads[0]['history']['games']), 12)
        difference = data['difference']
        assert isinstance(difference, dict)
        self.assertGreater(len(difference['games']), 1)

    def test_strength_is_shown_only_where_both_have_it(self) -> None:
        tune = create_test(self.user, test_mode='SPSA', **played())
        SPSARun.objects.create(
            tune=tune,
            reporting_type='BATCHED',
            distribution_type='SINGLE',
            alpha=0.602,
            gamma=0.101,
            iterations=100,
            pairs_per=8,
            a_ratio=0.1,
        )
        html = self.client.get(self.compare(self.a.id, tune.id)).content.decode()
        self.assertNotIn('Elo (95%)', html)
        self.assertNotIn('data-compare-chart="elo"', html)
        self.assertNotIn('data-compare-chart="llr"', html)
        self.assertIn('LLR (bounds)', html)
        self.assertIn(f'href="/tune/{tune.id}/"', html)
        data = self.payload(html)
        self.assertEqual((data['show_elo'], data['show_llr']), (False, False))

    def test_the_form_alone(self) -> None:
        response = self.client.get('/compare/')
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('compare-data', response.content.decode())

    def test_invalid_ids_are_a_validation_error(self) -> None:
        for a, b in (('x', self.b.id), (self.a.id, ''), (self.a.id, self.a.id), ('1' * 19, self.b.id)):
            with self.subTest(a=a, b=b):
                response = self.client.get(self.compare(a, b))
                self.assertEqual(response.status_code, 400)
                html = response.content.decode()
                self.assertIn('id="compare-errors"', html)
                self.assertEqual(inline_code(html), [])

    def test_unknown_workload_is_not_found(self) -> None:
        missing = Test.objects.order_by('-id').values_list('id', flat=True)[0] + 1
        self.assertEqual(self.client.get(self.compare(self.a.id, missing)).status_code, 404)
        self.assertEqual(self.client.get(self.compare(missing, self.a.id)).status_code, 404)

    def test_anonymous_viewers_are_sent_to_login_before_any_lookup(self) -> None:
        self.client.logout()
        with self.assertNumQueries(0):
            response = self.client.get(self.compare(self.a.id, self.b.id))
        self.assertRedirects(response, '/login/', fetch_redirect_response=False)

    def test_workload_page_links_to_compare_and_the_csv(self) -> None:
        html = self.client.get(f'/test/{self.a.id}/').content.decode()
        self.assertIn('action="/compare/"', html)
        self.assertIn(f'name="a" value="{self.a.id}"', html)
        self.assertIn(f'href="/api/workload/{self.a.id}/history.csv"', html)


class HistoryCsvViewTests(TestCase):
    def setUp(self) -> None:
        create_engine_config()
        ensure_book()
        self.user = create_user('reader')
        self.test = create_test(self.user, currentllr=1.2, **played())
        record_history(self.test, 6)
        self.url = f'/api/workload/{self.test.id}/history.csv'

    def test_download(self) -> None:
        self.client.force_login(self.user)
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'text/csv; charset=utf-8')
        self.assertEqual(response['Content-Disposition'], f'attachment; filename="workload-{self.test.id}-history.csv"')

        rows = list(csv.reader(io.StringIO(response.content.decode())))
        self.assertEqual(tuple(rows[0]), HISTORY_COLUMNS)
        insights = self.client.get(f'/api/workload/{self.test.id}/insights/').json()['insights']
        self.assertEqual([int(row[1]) for row in rows[1:]], [p['games'] for p in insights['history']['points']])
        for row in rows[1:]:
            for cell in row:
                self.assertRegex(cell, f'^(?:{SAFE_CELL.pattern})$')

    def test_credentials_in_post_are_accepted(self) -> None:
        self.assertEqual(self.client.post(self.url, credentials(self.user)).status_code, 200)

    def test_anonymous_is_rejected(self) -> None:
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 401)
        self.assertIn('error', response.json())

    def test_unknown_workload(self) -> None:
        self.client.force_login(self.user)
        response = self.client.get(f'/api/workload/{self.test.id + 1}/history.csv')
        self.assertEqual(response.status_code, 404)
        self.assertIn('error', response.json())
