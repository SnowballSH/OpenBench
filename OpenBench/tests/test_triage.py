import dataclasses
import json
import os
import tempfile
import time
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any, ClassVar
from unittest import mock
from urllib.parse import urlencode

from django.conf import settings
from django.contrib.auth.models import User
from django.http import QueryDict
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from OpenBench.models import LogEvent, Machine, Result, Test
from OpenBench.tests.datasets import SMALL, Dataset, DatasetSize, build_dataset
from OpenBench.tests.fixtures import (
    create_engine_config,
    create_test,
    create_user,
    credentials,
    ensure_book,
    present,
    system_info,
    use_temporary_media,
)
from OpenBench.tests.test_listing_rows import row_audit
from OpenBench.triage.actions import action_rows, operator_events
from OpenBench.triage.demo import BUILD_LOG, ILLEGAL_PGN, build_failure_summary, record_error, wrong_bench_summary
from OpenBench.triage.domain import Standing
from OpenBench.triage.groups import RAW_GROUP_LIMIT, error_events, group_rows, verdict, with_affected
from OpenBench.triage.kinds import ErrorKind, kind_condition, signature
from OpenBench.triage.logs import (
    MAX_FOLDED_LINES,
    VISIBLE_EDGE_LINES,
    displayable,
    excerpt,
    key_lines,
    log_lines,
    log_name,
    log_path,
    read_log,
)
from OpenBench.triage.query import KIND_OPTIONS, ErrorQuery, parse_limit

CLIENT = Path(settings.BASE_DIR) / 'Client'
DEV_SHA = '76f2da3c0b1e4d5a9c8b7a6f5e4d3c2b1a0f9e8d'
BATCH_CPU = 'AMD EPYC 9R14'

# Each summary exactly as the Client formats it, beside the source text that proves the format
CLIENT_SUMMARIES = (
    ('worker.py', "'%s build failed' % (final_name)", f'[Avalanche] {DEV_SHA} build failed', ErrorKind.BUILD),
    ('worker.py', "'[%s] %s' % (engine_name, branch_name)", '[Avalanche] lmr-tweak build failed', ErrorKind.BUILD),
    ('bench.py', "'[%s] Wrong Bench: %d' % (engine, benches[0])", '[Avalanche-76F2DA3C] Wrong Bench: 2412345', 'bench'),
    (
        'bench.py',
        "'[%s] Bench Exceeded Max Duration' % (binary)",
        '[Engines/A-76F2DA3C] Bench Exceeded Max Duration',
        'bench',
    ),
    ('bench.py', "'[%s] Failed to Execute Benchmark' % (engine)", '[A-76F2DA3C] Failed to Execute Benchmark', 'bench'),
    ('bench.py', "'[%s] Non-Deterministic Benches' % (engine)", '[A-76F2DA3C] Non-Deterministic Benches', 'bench'),
    ('worker.py', "return 'Disconnect'", 'Disconnect', ErrorKind.CRASH),
    ('worker.py', "return 'Stalled'", 'Stalled', ErrorKind.CRASH),
    ('worker.py', "return 'Illegal Move'", 'Illegal Move', ErrorKind.ILLEGAL),
    (
        'genfens.py',
        "'[%s] Stalled during genfens' % (args['engine'])",
        '[Engines/A-1] Stalled during genfens',
        'genfens',
    ),
)

ODD_SUMMARIES = (
    'Time Loss',
    'timeloss',
    'event 3',
    '',
    'Disconnect\n',
    ' Stalled',
    '[a] b] Failed to Execute Benchmark',
    '[Avalanche-76F2DA3C] Wrong Bench: twelve',
    '[Avalanche] two\nlines build failed',
    'build failed',
    '[x] Wrong Bench: 1 build failed',
    'Illegal move',
)


class ClassificationTests(SimpleTestCase):
    def test_every_client_summary_is_classified(self) -> None:
        for source, fragment, summary, kind in CLIENT_SUMMARIES:
            with self.subTest(summary=summary):
                self.assertIn(fragment, (CLIENT / source).read_text())
                self.assertEqual(signature(summary).kind, kind)

    def test_a_build_failure_is_titled_by_engine_and_keyed_by_branch(self) -> None:
        found = signature(f'[Avalanche] {DEV_SHA} build failed')
        self.assertEqual((found.title, found.subject), ('Avalanche build failed', '76f2da3c'))
        self.assertEqual(signature('[Avalanche] lmr-tweak build failed').subject, 'lmr-tweak')

    def test_a_wrong_bench_keeps_its_binary_and_number(self) -> None:
        found = signature('[Avalanche-76F2DA3C] Wrong Bench: 2412345')
        self.assertEqual((found.title, found.subject, found.bench), ('Wrong Bench', 'Avalanche-76F2DA3C', 2412345))

    def test_time_losses_and_unknown_text(self) -> None:
        self.assertEqual(signature('timeloss').kind, ErrorKind.TIME_LOSS)
        self.assertEqual(signature('Something else').kind, ErrorKind.OTHER)
        self.assertEqual(signature('[x] Something else').title, 'Something else')


class KindConditionTests(TestCase):
    def test_the_database_filter_agrees_with_the_classifier(self) -> None:
        summaries = [summary for _, _, summary, _ in CLIENT_SUMMARIES] + list(ODD_SUMMARIES)
        LogEvent.objects.bulk_create(
            LogEvent(author='w', summary=summary, log_file='', machine_id=1, test_id=1) for summary in summaries
        )
        for kind in ErrorKind:
            with self.subTest(kind=kind):
                matched = LogEvent.objects.filter(kind_condition(frozenset({kind})))
                self.assertEqual(
                    sorted(matched.values_list('summary', flat=True)),
                    sorted(summary for summary in summaries if signature(summary).kind is kind),
                )

    def test_every_kind_has_a_filter_option(self) -> None:
        self.assertLessEqual({kind.value for kind in ErrorKind}, {option.value for option in KIND_OPTIONS})


class QueryTests(SimpleTestCase):
    def parse(self, text: str) -> ErrorQuery:
        return ErrorQuery.parse(QueryDict(text))

    def test_parses_and_round_trips(self) -> None:
        query = self.parse('workload=9&kind=build&unresolved=1&view=list')
        self.assertEqual((query.workload, query.kind, query.unresolved, query.is_list), (9, 'build', True, True))
        self.assertEqual(query.querystring, '?workload=9&kind=build&unresolved=1&view=list')
        self.assertEqual(self.parse('').querystring, '')

    def test_bad_values_fall_back(self) -> None:
        query = self.parse('workload=9' + '9' * 30 + '&kind=<script>&view=grid&unresolved=maybe')
        self.assertEqual(query, ErrorQuery())
        self.assertEqual(self.parse('workload=١٢').workload, None)

    def test_game_covers_the_game_kinds(self) -> None:
        self.assertEqual(self.parse('kind=game').kinds, {ErrorKind.CRASH, ErrorKind.TIME_LOSS, ErrorKind.ILLEGAL})

    def test_limit_is_clamped(self) -> None:
        self.assertEqual([parse_limit(value) for value in (None, 'x', '0', '7', '5000')], [25, 25, 1, 7, 100])


class TriageCase(TestCase):
    author: ClassVar[User]

    @classmethod
    def setUpTestData(cls) -> None:
        create_engine_config()
        ensure_book()
        cls.author = create_user('author')

    def setUp(self) -> None:
        self.media = Path(use_temporary_media(self))
        self.now = timezone.now()
        self.client.force_login(self.author)

    def pinned(self, **fields: Any) -> Test:
        test = create_test(self.author, info='Scale LMR by <history>\navl:76f2da3c0b1e', **fields)
        test.dev.name = test.dev.sha = DEV_SHA
        test.dev.bench = 2400000
        test.dev.save()
        return test

    def batch_machine(self, index: int = 0, **info: Any) -> Machine:
        named = system_info(
            machine_name=f'batch-{uuid.UUID(int=index + 1)}:0', cpu_name=BATCH_CPU, os_ver='6.8', **info
        )
        return Machine.objects.create(user=self.author, info=named)

    def error(
        self, test: Test, summary: str, minutes_ago: float = 60, log: str | None = None, machine: Machine | None = None
    ) -> LogEvent:
        machine = machine or self.batch_machine(LogEvent.objects.count())
        return record_error(test, machine.id, 'author', summary, self.now - timedelta(minutes=minutes_ago), log)

    def played(self, test: Test, minutes_ago: float, games: int = 10) -> None:
        result = Result.objects.create(test=test, machine=self.batch_machine(900 + Result.objects.count()), games=games)
        Result.objects.filter(id=result.id).update(updated=self.now - timedelta(minutes=minutes_ago))

    def rows(self, text: str = '') -> list[Any]:
        query = ErrorQuery.parse(QueryDict(text))
        rows, _ = group_rows(query, self.now)
        return list(with_affected(rows, error_events(query)))


class GroupingTests(TriageCase):
    def test_a_failure_repeated_on_every_worker_is_one_group(self) -> None:
        test = self.pinned()
        events = [self.error(test, build_failure_summary(test), minutes, BUILD_LOG) for minutes in range(14, 0, -1)]
        Machine.objects.filter(id=events[0].machine_id).delete()

        (row,) = self.rows()

        self.assertEqual((row.group.count, row.group.signature.title), (14, 'Avalanche build failed'))
        self.assertEqual((row.group.first_seen, row.group.last_seen), (events[0].created, events[-1].created))
        self.assertEqual((row.first_ago, row.last_ago), ('14m ago', '1m ago'))
        self.assertEqual(row.log_event_id, events[-1].id)
        self.assertEqual((row.affected.registrations, row.affected.hosts, row.affected.pruned), (14, 13, 1))
        self.assertEqual(
            [(pool.label, pool.cpu_name, pool.hosts) for pool in row.affected.pools], [('batch-*', BATCH_CPU, 13)]
        )
        self.assertFalse(row.affected.sampled)

    def test_dev_and_base_failures_and_other_workloads_stay_apart(self) -> None:
        test, other = self.pinned(), self.pinned()
        self.error(test, build_failure_summary(test))
        self.error(test, '[Avalanche] master build failed')
        self.error(other, build_failure_summary(other))

        self.assertEqual(
            sorted((row.group.test_id, row.group.signature.subject) for row in self.rows()),
            sorted([(test.id, '76f2da3c'), (test.id, 'master'), (other.id, '76f2da3c')]),
        )

    def test_wrong_benches_with_different_numbers_are_one_group(self) -> None:
        test = self.pinned()
        self.error(test, wrong_bench_summary(test, 2412345), 30)
        self.error(test, wrong_bench_summary(test, 2399999), 20)

        (row,) = self.rows()

        self.assertEqual((row.group.count, sorted(row.group.benches)), (2, [2399999, 2412345]))
        self.assertEqual((row.bench.got, row.bench.expected, row.bench.difference), (2399999, 2400000, -1))
        self.assertIsNone(row.log_event_id)

    def test_groups_are_newest_first_and_operator_actions_are_left_out(self) -> None:
        test = self.pinned()
        self.error(test, 'Disconnect', 50)
        self.error(test, 'Illegal Move', 5)
        LogEvent.objects.create(author='author', summary='STOP', log_file='', test_id=test.id)

        self.assertEqual([row.group.signature.title for row in self.rows()], ['Illegal Move', 'Disconnect'])

    def test_a_missing_workload_is_reported_not_raised(self) -> None:
        test = self.pinned()
        self.error(test, 'Disconnect')
        LogEvent.objects.update(test_id=987654)

        (row,) = self.rows()

        self.assertIsNone(row.workload)
        self.assertEqual(row.verdict.reason, 'workload no longer exists')
        self.assertContains(self.client.get('/errors/'), '#987654 (removed)')
        self.assertContains(self.client.get('/errors/?view=list'), '#987654 (removed)')

    def test_the_flat_list_links_only_registered_machines(self) -> None:
        self.error(self.pinned(), 'Disconnect')
        machine_id = LogEvent.objects.values_list('machine_id', flat=True).first()
        self.assertContains(self.client.get('/errors/?view=list'), f'href="/machines/{machine_id}/"')

        Machine.objects.filter(id=machine_id).delete()

        html = self.client.get('/errors/?view=list').content.decode()
        self.assertNotIn(f'href="/machines/{machine_id}/"', html)
        self.assertIn('(no longer registered)', html)

    def test_only_the_newest_summaries_are_grouped(self) -> None:
        test = self.pinned()
        machine = self.batch_machine()
        LogEvent.objects.bulk_create(
            LogEvent(author='author', summary=f'odd {index}', log_file='', machine_id=machine.id, test_id=test.id)
            for index in range(RAW_GROUP_LIMIT + 3)
        )

        rows, truncated = group_rows(ErrorQuery(), self.now)

        self.assertEqual((len(rows), truncated), (RAW_GROUP_LIMIT, True))
        self.assertContains(self.client.get('/errors/'), 'Grouping the 500 most recently seen summaries')


class StandingTests(TriageCase):
    def standing(self, test: Test, minutes_ago: float = 60) -> tuple[str, str]:
        self.error(test, 'x', minutes_ago)
        (row,) = self.rows()
        return row.verdict.standing, row.verdict.reason

    def test_a_recent_error_is_still_happening(self) -> None:
        self.assertEqual(self.standing(self.pinned(), minutes_ago=3)[0], Standing.HAPPENING)

    def test_results_after_the_last_error_resolve_it(self) -> None:
        test = self.pinned()
        self.played(test, minutes_ago=20)
        self.assertEqual(self.standing(test, minutes_ago=40), (Standing.RESOLVED, 'results arrived since'))

    def test_results_before_the_last_error_do_not(self) -> None:
        test = self.pinned()
        self.played(test, minutes_ago=90)
        self.assertEqual(self.standing(test, minutes_ago=40)[0], Standing.QUIET)

    def test_an_assignment_without_games_is_not_a_result(self) -> None:
        test = self.pinned()
        self.played(test, minutes_ago=20, games=0)
        self.assertEqual(self.standing(test, minutes_ago=40)[0], Standing.QUIET)

    def test_a_finished_or_deleted_workload_is_resolved_even_when_recent(self) -> None:
        self.assertEqual(
            self.standing(self.pinned(finished=True), minutes_ago=1), (Standing.RESOLVED, 'workload finished')
        )
        LogEvent.objects.all().delete()
        self.assertEqual(
            self.standing(self.pinned(deleted=True), minutes_ago=1), (Standing.RESOLVED, 'workload deleted')
        )

    def test_the_boundary_is_the_diagnosis_window(self) -> None:
        test = self.pinned()
        self.error(test, 'x')
        (row,) = self.rows()
        group = dataclasses.replace(row.group, last_seen=self.now - timedelta(minutes=10))
        self.assertEqual(verdict(group, test, None, self.now).standing, Standing.HAPPENING)
        self.assertEqual(verdict(group, test, None, self.now + timedelta(seconds=1)).standing, Standing.QUIET)


class FilterTests(TriageCase):
    def setUp(self) -> None:
        super().setUp()
        self.building, self.playing, self.done = self.pinned(), self.pinned(), self.pinned(finished=True)
        self.error(self.building, build_failure_summary(self.building), 2, BUILD_LOG)
        self.error(self.playing, 'Disconnect', 70, ILLEGAL_PGN)
        self.error(self.playing, 'Illegal Move', 80, ILLEGAL_PGN)
        self.error(self.done, wrong_bench_summary(self.done, 1), 90)
        self.error(self.done, 'unheard of', 95)

    def titles(self, text: str) -> list[str]:
        return [row.group.signature.title for row in self.rows(text)]

    def test_by_workload(self) -> None:
        self.assertEqual(self.titles(f'workload={self.playing.id}'), ['Disconnect', 'Illegal Move'])
        self.assertEqual(self.titles('workload=424242'), [])

    def test_by_kind(self) -> None:
        self.assertEqual(self.titles('kind=build'), ['Avalanche build failed'])
        self.assertEqual(self.titles('kind=bench'), ['Wrong Bench'])
        self.assertEqual(self.titles('kind=game'), ['Disconnect', 'Illegal Move'])
        self.assertEqual(self.titles('kind=crash'), ['Disconnect'])
        self.assertEqual(self.titles('kind=illegal'), ['Illegal Move'])
        self.assertEqual(self.titles('kind=other'), ['unheard of'])
        self.assertEqual(len(self.titles('kind=nonsense')), 5)

    def test_only_unresolved(self) -> None:
        self.assertEqual(self.titles('unresolved=1'), ['Avalanche build failed', 'Disconnect', 'Illegal Move'])
        self.played(self.playing, minutes_ago=30)
        self.assertEqual(self.titles('unresolved=1'), ['Avalanche build failed'])

    def test_the_page_applies_the_filters_and_links_keep_them(self) -> None:
        html = self.client.get(f'/errors/?workload={self.playing.id}&kind=crash').content.decode()
        self.assertIn('Disconnect', html)
        self.assertNotIn('Illegal Move', html)
        self.assertNotIn('build failed', html)
        self.assertIn(f'href="/errors/?workload={self.playing.id}&amp;kind=crash&amp;view=list"', html)
        self.assertIn(f'href="/errors/?workload={self.playing.id}&amp;kind=crash&amp;unresolved=1"', html)
        self.assertIn('href="/errors/?kind=crash">Show every workload</a>', html)

    def test_the_flat_list_is_filtered_and_paged(self) -> None:
        for _ in range(30):
            self.error(self.playing, 'Stalled', 200)
        first = self.client.get(f'/errors/?view=list&kind=crash&workload={self.playing.id}').content.decode()
        self.assertEqual(first.count('<td class="triage-summary">'), 25)
        self.assertNotIn('Illegal Move', first)
        self.assertIn(f'href="/errors/2/?workload={self.playing.id}&amp;kind=crash&amp;view=list"', first)
        second = self.client.get(f'/errors/2/?view=list&kind=crash&workload={self.playing.id}').content.decode()
        self.assertEqual(second.count('<td class="triage-summary">'), 6)

    def test_grouped_pages_hold_twenty_five_groups(self) -> None:
        for index in range(30):
            self.error(self.playing, f'odd {index}', 300)
        self.assertEqual(self.client.get('/errors/').content.decode().count('class="triage-error"'), 25)
        self.assertEqual(self.client.get('/errors/2/').content.decode().count('class="triage-error"'), 10)

    def test_rows_are_navigable(self) -> None:
        for url in ('/errors/', '/errors/?view=list'):
            with self.subTest(url=url):
                audit = row_audit(self.client.get(url).content.decode())
                self.assertEqual(audit.defects(), [])
                self.assertEqual(len(audit.rows), 5)

    def test_a_readable_title_names_the_workload(self) -> None:
        html = self.client.get('/errors/').content.decode()
        self.assertIn(f'<span class="row-id">#{self.building.id}</span> Scale LMR by &lt;history&gt;</a>', html)


class EventPageTests(TriageCase):
    def test_header_names_what_when_where(self) -> None:
        test = self.pinned()
        machine = self.batch_machine(7, compilers={'Avalanche': ['zig', '0.16.0']})
        event = self.error(test, build_failure_summary(test), 5, BUILD_LOG, machine)
        self.error(test, build_failure_summary(test), 6, BUILD_LOG)

        html = self.client.get(f'/event/{event.id}/').content.decode()

        self.assertIn('<h1>Avalanche build failed</h1>', html)
        self.assertIn(f'[Avalanche] {DEV_SHA} build failed', html)
        self.assertIn('>5m ago</time>', html)
        self.assertIn(f'<span class="row-id">#{test.id}</span> Scale LMR by &lt;history&gt;</a>', html)
        listed = urlencode({'workload': test.id, 'summary': event.summary, 'view': 'list'}).replace('&', '&amp;')
        self.assertIn(f'href="/errors/?{listed}">2 with this summary', html)
        self.assertIn(f'<a href="/machines/{machine.id}/">machine {machine.id}</a>', html)
        self.assertIn('<td>batch-*</td>', html)
        self.assertIn(f'{BATCH_CPU}, 4 threads', html)
        self.assertIn('<td>Linux 6.8</td>', html)
        self.assertIn('<div>Avalanche: zig 0.16.0</div>', html)

    def test_a_pruned_machine_is_said_to_be_gone(self) -> None:
        event = self.error(self.pinned(), 'Disconnect', log=ILLEGAL_PGN)
        Machine.objects.filter(id=event.machine_id).delete()
        html = self.client.get(f'/event/{event.id}/').content.decode()
        self.assertIn(f'machine {event.machine_id} <span class="muted">(no longer registered)</span>', html)
        self.assertNotIn('>Pool<', html)

    def test_key_lines_lead_the_log(self) -> None:
        event = self.error(self.pinned(), '[Avalanche] x build failed', log=BUILD_LOG)
        html = self.client.get(f'/event/{event.id}/').content.decode()
        self.assertIn('href="#L5"', html)
        self.assertIn('error: expected type &#x27;@Vector(32, i16)&#x27;, found &#x27;@Vector(16, i16)&#x27;', html)
        self.assertIn('make: *** [Makefile:14: all] Error 1', html)
        self.assertLess(html.index('id="key-lines-title"'), html.index('id="log-title"'))

    def test_a_wrong_bench_shows_both_numbers_and_the_difference(self) -> None:
        test = self.pinned()
        event = self.error(test, wrong_bench_summary(test, 2418422))
        html = self.client.get(f'/event/{event.id}/').content.decode()
        self.assertIn('expected 2400000, got 2418422 (+18422)', html)
        self.assertIn('The worker sent no log with this error.', html)

    def test_log_text_is_escaped_and_never_linked(self) -> None:
        hostile = '<script>alert(1)</script>\n<a href="https://evil.example/">x</a> https://evil.example/\n</div></pre>'
        test = self.pinned()
        event = self.error(test, '<img src=x onerror=alert(1)>', log=hostile)
        html = self.client.get(f'/event/{event.id}/').content.decode()
        self.assertNotIn('<script>alert', html)
        self.assertNotIn('<img src=x', html)
        self.assertNotIn('href="https://evil.example', html)
        self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt;', html)
        self.assertIn('&lt;img src=x onerror=alert(1)&gt;', self.client.get('/errors/').content.decode())

    def test_a_long_log_is_folded(self) -> None:
        lines = 2 * VISIBLE_EDGE_LINES + MAX_FOLDED_LINES + 40
        event = self.error(self.pinned(), 'Disconnect', log='\n'.join(f'line {n}' for n in range(1, lines + 1)))
        html = self.client.get(f'/event/{event.id}/').content.decode()
        self.assertIn(f'<summary>{MAX_FOLDED_LINES:,} more lines</summary>', html)
        self.assertIn('40 lines left out here', html)
        self.assertIn(f'id="L{lines}" data-line="{lines}">line {lines}</div>', html)
        self.assertNotIn(f'id="L{VISIBLE_EDGE_LINES + MAX_FOLDED_LINES + 1}"', html)
        self.assertLess(html.index(f'id="L{VISIBLE_EDGE_LINES}"'), html.index('<details class="log-fold">'))

    def test_a_truncated_log_says_so(self) -> None:
        event = self.error(self.pinned(), 'Disconnect', log='0123456789\n' * 100)
        with mock.patch('OpenBench.triage.logs.MAX_LOG_BYTES', 110):
            html = self.client.get(f'/event/{event.id}/').content.decode()
        self.assertIn('Only the first 110\xa0bytes of this 1.1\xa0KB log are shown', html)
        self.assertIn('id="L10"', html)
        self.assertNotIn('id="L11"', html)

    def test_a_missing_file_and_an_operator_action(self) -> None:
        test = self.pinned()
        event = self.error(test, 'Disconnect', log='x')
        (self.media / log_name(event.id)).unlink()
        self.assertContains(self.client.get(f'/event/{event.id}/'), 'The log file for this error is no longer stored.')

        action = LogEvent.objects.create(author='author', summary='STOP', log_file='', test_id=test.id)
        self.assertRedirects(self.client.get(f'/event/{action.id}/'), '/errors/')
        self.assertRedirects(self.client.get('/event/999999/'), '/errors/')


class RawLogTests(TriageCase):
    def test_download_headers(self) -> None:
        event = self.error(self.pinned(), 'Disconnect', log='<b>héllo</b>\n')
        for url in (f'/event/{event.id}/raw', f'/event/{event.id}/raw/'):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response['Content-Type'], 'text/plain; charset=utf-8')
            self.assertEqual(response['Content-Disposition'], f'attachment; filename="event{event.id}.log"')
            self.assertEqual(response['X-Content-Type-Options'], 'nosniff')
            self.assertEqual(response.getvalue(), '<b>héllo</b>\n'.encode())

    def test_only_get_and_head(self) -> None:
        event = self.error(self.pinned(), 'Disconnect', log='x')
        self.assertEqual(self.client.post(f'/event/{event.id}/raw').status_code, 405)
        self.assertEqual(self.client.head(f'/event/{event.id}/raw').status_code, 200)

    def test_only_the_events_own_file_is_ever_read(self) -> None:
        test = self.pinned()
        secret = self.media.parent / f'{self.media.name}-secret.txt'
        secret.write_text('secret')
        self.addCleanup(secret.unlink)
        (self.media / 'other.log').write_text('other')

        for stored in (f'../{secret.name}', str(secret), 'other.log', 'event1.log/../other.log', ''):
            with self.subTest(stored=stored):
                event = self.error(test, 'Disconnect')
                LogEvent.objects.filter(id=event.id).update(log_file=stored)
                event.refresh_from_db()
                self.assertIsNone(log_path(event))
                self.assertRedirects(self.client.get(f'/event/{event.id}/raw'), '/errors/')
                self.assertNotContains(self.client.get(f'/event/{event.id}/'), 'secret')

    def test_a_symlink_is_not_followed(self) -> None:
        event = self.error(self.pinned(), 'Disconnect', log='x')
        target = self.media / 'elsewhere'
        target.write_text('elsewhere')
        (self.media / log_name(event.id)).unlink()
        os.symlink(target, self.media / log_name(event.id))
        self.assertIsNone(log_path(event))

    def test_operator_actions_have_no_raw_log(self) -> None:
        action = LogEvent.objects.create(author='author', summary='STOP', log_file='event1.log', test_id=1)
        (self.media / log_name(action.id)).write_text('x')
        self.assertRedirects(self.client.get(f'/event/{action.id}/raw'), '/errors/')

    def test_an_anonymous_viewer_is_sent_to_the_login(self) -> None:
        event = self.error(self.pinned(), 'Disconnect', log='x')
        self.client.logout()
        for url in (f'/event/{event.id}/raw', f'/event/{event.id}/', '/errors/', '/events/'):
            with self.subTest(url=url):
                self.assertRedirects(self.client.get(url), '/login/')


class LogTextTests(SimpleTestCase):
    def test_reading_is_capped(self) -> None:
        path = Path(self.enterContext(tempfile.TemporaryDirectory())) / 'event1.log'
        path.write_bytes(b'abc\xff' * 10)
        log = read_log(path, limit=8)
        self.assertEqual((log.text, log.size, log.truncated), ('abc�abc�', 40, True))
        self.assertFalse(read_log(path).truncated)

    def test_control_sequences_are_defused(self) -> None:
        self.assertEqual(displayable('\x1b[31;1merror:\x1b[0m bad\x07\tend'), 'error: bad�\tend')
        self.assertEqual(displayable('a‮b'), 'a�b')

    def test_short_logs_are_shown_whole(self) -> None:
        shown = excerpt(log_lines('a\nb\r\nc'))
        self.assertEqual([line.text for line in shown.head], ['a', 'b', 'c'])
        self.assertEqual((shown.folded, shown.tail, shown.omitted, shown.total), ((), (), 0, 3))

    def test_key_lines_from_a_traceback_and_a_game(self) -> None:
        text = '\n'.join(
            [
                'starting',
                'Traceback (most recent call last):',
                '  File "worker.py", line 12, in run',
                '    build()',
                '  File "utils.py", line 301, in build',
                '    raise OpenBenchBuildFailedException(message, comp_output)',
                'utils.OpenBenchBuildFailedException: Error during compilation',
                'lib.cpp:3:1: fatal error: nnue.h: No such file or directory',
                'ld: undefined reference to `evaluate`',
                '[Termination "illegal move"]',
                '{Black makes an Illegal Move: b6e7}',
                'error is a word here',
            ]
        )
        lines = log_lines(text)
        found = key_lines(lines, frozenset({2, 3}))
        self.assertEqual(
            [(line.number, line.label) for line in found],
            [
                (2, 'traceback'),
                (3, 'traceback'),
                (4, 'traceback'),
                (5, 'traceback'),
                (7, 'traceback'),
                (8, 'error'),
                (9, 'error'),
                (10, 'illegal move'),
                (11, 'illegal move'),
            ],
        )
        self.assertEqual([line.shown for line in found][:4], [True, True, False, False])

    def test_key_lines_are_bounded(self) -> None:
        lines = log_lines('\n'.join(['x.zig:1:1: error: ' + 'y' * 1000] * 500))
        found = key_lines(lines, frozenset())
        self.assertEqual((len(found), len(found[0].text)), (30, 240))


class OperatorEventTests(TriageCase):
    def action(self, test: Test, summary: str, author: str = 'author') -> LogEvent:
        return LogEvent.objects.create(author=author, summary=summary, log_file='', test_id=test.id)

    def test_consecutive_repeats_collapse(self) -> None:
        test, other = self.pinned(), self.pinned()
        self.action(test, 'CREATE P=0 TP=1000')
        first = self.action(test, 'MODIFY')
        self.action(test, 'MODIFY')
        last = self.action(test, 'MODIFY')
        self.action(other, 'MODIFY')
        self.action(test, 'MODIFY', author='someone')
        self.action(test, 'MODIFY')

        rows = action_rows(list(operator_events()), self.now)

        self.assertEqual(
            [(row.event.summary, row.event.test_id, row.event.author, row.count) for row in rows],
            [
                ('MODIFY', test.id, 'author', 1),
                ('MODIFY', test.id, 'someone', 1),
                ('MODIFY', other.id, 'author', 1),
                ('MODIFY', test.id, 'author', 3),
                ('CREATE P=0 TP=1000', test.id, 'author', 1),
            ],
        )
        self.assertEqual((rows[3].event.id, rows[3].first_at), (last.id, first.created))

    def test_the_page_names_the_workload_and_dates_relatively(self) -> None:
        test = self.pinned()
        for _ in range(3):
            self.action(test, 'MODIFY')
        html = self.client.get('/events/').content.decode()
        self.assertIn(f'<span class="row-id">#{test.id}</span> Scale LMR by &lt;history&gt;</a>', html)
        self.assertIn('<div class="row-meta mono">76f2da3c vs base</div>', html)
        self.assertIn('>just now</time>', html)
        self.assertRegex(html, r'MODIFY <span class="badge" title="3 in a row, the first at [^"]+">&times;3</span>')
        audit = row_audit(html)
        self.assertEqual((audit.defects(), len(audit.rows)), ([], 1))

    def test_worker_errors_are_left_out(self) -> None:
        self.error(self.pinned(), 'Disconnect')
        self.assertEqual(row_audit(self.client.get('/events/').content.decode()).rows, [])


class WorkloadHookTests(TriageCase):
    def test_the_workload_page_counts_its_worker_errors(self) -> None:
        test = self.pinned()
        self.assertNotContains(self.client.get(f'/test/{test.id}/'), 'Worker errors')
        self.error(test, 'Disconnect')
        self.error(test, 'Stalled')
        LogEvent.objects.create(author='author', summary='STOP', log_file='', test_id=test.id)
        self.assertContains(
            self.client.get(f'/test/{test.id}/'), f'<a href="/errors/?workload={test.id}">Worker errors (2)</a>'
        )


class ApiTests(TriageCase):
    def setUp(self) -> None:
        super().setUp()
        self.test = self.pinned()
        self.logged = self.error(self.test, build_failure_summary(self.test), 3, BUILD_LOG)
        self.error(self.test, wrong_bench_summary(self.test, 2418422), 200)
        self.error(self.pinned(finished=True), 'Disconnect', 300)

    def groups(self, text: str = '') -> list[dict[str, Any]]:
        response = self.client.get(f'/api/errors/{text}')
        self.assertEqual(response.status_code, 200)
        groups: list[dict[str, Any]] = json.loads(response.content)['groups']
        return groups

    def test_groups_say_what_is_failing(self) -> None:
        build, bench, crash = self.groups()
        self.assertEqual(
            {key: build[key] for key in ('kind', 'title', 'subject', 'count', 'status', 'latest_event', 'log_url')},
            {
                'kind': 'build',
                'title': 'Avalanche build failed',
                'subject': '76f2da3c',
                'count': 1,
                'status': 'happening',
                'latest_event': self.logged.id,
                'log_url': f'/api/errors/{self.logged.id}/log/',
            },
        )
        self.assertEqual(
            build['workload'],
            {
                'id': self.test.id,
                'exists': True,
                'url': f'/test/{self.test.id}/',
                'title': 'Scale LMR by <history>',
                'commits': '76f2da3c vs base',
                'engine': 'Avalanche',
                'time_control': '8.0+0.08',
                'finished': False,
                'deleted': False,
                'games': 0,
            },
        )
        self.assertEqual(build['last_seen'], self.logged.created.isoformat())
        self.assertEqual(
            build['affected'],
            {
                'registrations': 1,
                'hosts': 1,
                'pruned': 0,
                'sampled': False,
                'pools': [{'label': 'batch-*', 'cpu': BATCH_CPU, 'hosts': 1}],
            },
        )
        self.assertEqual(bench['bench'], {'reported': [2418422], 'expected': 2400000, 'difference': 18422})
        self.assertEqual((bench['status'], bench['log_url'], build['bench']), ('quiet', None, None))
        self.assertEqual((crash['status'], crash['reason']), ('resolved', 'workload finished'))

    def test_the_log_of_a_group_is_fetched_with_the_same_credentials(self) -> None:
        url = f'/api/errors/{self.logged.id}/log/'
        self.client.logout()
        self.assertEqual(self.client.get(url).status_code, 401)
        response = self.client.post(url, credentials(self.author))
        self.assertEqual(response.getvalue().decode(), BUILD_LOG)
        self.assertEqual(response['Content-Type'], 'text/plain; charset=utf-8')
        self.assertEqual(response['Content-Disposition'], f'attachment; filename="event{self.logged.id}.log"')
        missing = self.client.post('/api/errors/999999/log/', credentials(self.author))
        self.assertEqual(
            (missing.status_code, json.loads(missing.content)), (404, {'error': 'No logs for event exist'})
        )

    def test_filters_and_limit(self) -> None:
        self.assertEqual([group['kind'] for group in self.groups(f'?workload={self.test.id}')], ['build', 'bench'])
        self.assertEqual([group['kind'] for group in self.groups('?kind=bench')], ['bench'])
        self.assertEqual([group['kind'] for group in self.groups('?unresolved=1')], ['build', 'bench'])
        self.assertEqual([group['kind'] for group in self.groups('?limit=1')], ['build'])
        body = json.loads(self.client.get('/api/errors/?limit=1').content)
        self.assertEqual((body['total'], body['truncated']), (3, False))

    def test_credentials_in_a_post_body(self) -> None:
        self.client.logout()
        self.assertEqual(self.client.get('/api/errors/').status_code, 401)
        self.assertEqual(self.client.post('/api/errors/', {'username': 'author', 'password': 'wrong'}).status_code, 401)
        response = self.client.post('/api/errors/?kind=bench', {**credentials(self.author), 'limit': '5'})
        self.assertEqual([group['kind'] for group in json.loads(response.content)['groups']], ['bench'])
        posted = self.client.post('/api/errors/', {**credentials(self.author), 'kind': 'build'})
        self.assertEqual([group['kind'] for group in json.loads(posted.content)['groups']], ['build'])


def error_dataset(size: DatasetSize) -> Dataset:
    data = build_dataset(size)
    now = timezone.now()
    for index, machine in enumerate(data.machines):
        test = data.tests[index % 7]
        summary = (build_failure_summary(test), 'Disconnect', wrong_bench_summary(test, index))[index % 3]
        record_error(test, machine.id, 'user0', summary, now - timedelta(minutes=index), None)
    return data


class TriageQueryBudgetTests(TestCase):
    size: ClassVar[DatasetSize] = SMALL
    data: ClassVar[Dataset]
    budgets: ClassVar[dict[str, int]] = {
        '/errors/': 9,
        '/errors/?kind=game&unresolved=1': 9,
        '/errors/?view=list': 8,
        '/api/errors/?limit=100': 8,
        '/events/': 7,
    }

    @classmethod
    def setUpTestData(cls) -> None:
        cls.data = error_dataset(cls.size)

    def setUp(self) -> None:
        use_temporary_media(self)
        self.client.force_login(self.data.users[0])

    def test_pages(self) -> None:
        for url, queries in self.budgets.items():
            with self.subTest(url=url), self.assertNumQueries(queries):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_event_page(self) -> None:
        event = present(LogEvent.objects.filter(machine_id__gt=0).order_by('-id').first())
        with self.assertNumQueries(8):
            self.assertEqual(self.client.get(f'/event/{event.id}/').status_code, 200)


class LargerTriageQueryBudgetTests(TriageQueryBudgetTests):
    size = dataclasses.replace(SMALL, tests=160, machines=400, events=300)


HOSTILE_SIZE = 2 * 1024 * 1024
HOSTILE_SECONDS = 2.0


class HostileLogTests(SimpleTestCase):
    def assert_fast(self, text: str) -> None:
        started = time.monotonic()
        lines = log_lines(text)
        key_lines(lines, excerpt(lines).shown_numbers)
        self.assertLess(time.monotonic() - started, HOSTILE_SECONDS)

    def test_a_line_of_make_error_prefixes(self) -> None:
        self.assert_fast('*** [' * (HOSTILE_SIZE // 5))

    def test_many_long_lines_of_make_error_prefixes(self) -> None:
        self.assert_fast(('*** [' * 800 + '\n') * (HOSTILE_SIZE // 4001))

    def test_indented_traceback_headers_to_the_end(self) -> None:
        line = '  Traceback (most recent call last):\n'
        self.assert_fast(line * (HOSTILE_SIZE // len(line)))

    def test_traceback_headers_between_indented_lines(self) -> None:
        block = 'Traceback (most recent call last):\n' + '  frame\n' * 3
        self.assert_fast('x\n' + block * 20 + '  frame\n' * (HOSTILE_SIZE // 8))

    def test_unterminated_terminal_sequences(self) -> None:
        self.assert_fast('\x1b[' * (HOSTILE_SIZE // 4) + '\n' + '\x1b]0;' * (HOSTILE_SIZE // 8))

    def test_a_real_make_error_is_still_a_key_line(self) -> None:
        found = key_lines(log_lines('ok\nmake: *** [Makefile:14: all] Error 1\n'), frozenset())
        self.assertEqual([(line.number, line.label) for line in found], [(2, 'error')])

    def test_a_traceback_ends_at_a_nearby_exception_only(self) -> None:
        text = 'Traceback (most recent call last):\n' + '  frame\n' * 300 + 'ValueError: far away\n'
        self.assertEqual([line.number for line in key_lines(log_lines(text), frozenset())], [1, 2, 3, 4])


class SanitiserTests(SimpleTestCase):
    def test_lines_are_numbered_as_the_raw_file_numbers_them(self) -> None:
        lines = log_lines('a\rb\x0cc\x1cd\x85e\u2028f\u2029g\r\nh\n')
        self.assertEqual([line.number for line in lines], [1, 2])
        self.assertEqual(lines[0].text, 'a\ufffdb\ufffdc\ufffdd\ufffde\ufffdf\ufffdg')
        self.assertEqual(lines[1].text, 'h')
        self.assertEqual(log_lines(''), [])
        self.assertEqual([line.text for line in log_lines('\n\nx')], ['', '', 'x'])

    def test_terminal_sequences_and_invisible_controls(self) -> None:
        self.assertEqual(displayable('\x1b]0;title\x07shown'), 'shown')
        self.assertEqual(displayable('\x1b]8;;https://evil.example/\x1b\\link\x1b]8;;\x1b\\'), 'link')
        self.assertEqual(displayable('\x1b]0;never closed'), '')
        self.assertEqual(displayable('a\x9bb\x80c\x9fd'), 'a\ufffdb\ufffdc\ufffdd')
        self.assertEqual(displayable('a\u200eb\u200fc\u061cd\u2066e'), 'a\ufffdb\ufffdc\ufffdd\ufffde')
        self.assertEqual(displayable('tab\tand é and 中'), 'tab\tand é and 中')


class SummaryShapeTests(TriageCase):
    def test_an_empty_summary_gets_a_placeholder(self) -> None:
        for summary in ('', '   ', '[A] ', '[A]  '):
            with self.subTest(summary=summary):
                self.assertEqual(signature(summary).title, '(no summary)')

        event = self.error(self.pinned(), '', log='x')
        self.assertIn('>(no summary)<span class="visually-hidden">', self.client.get('/errors/').content.decode())
        self.assertContains(self.client.get(f'/event/{event.id}/'), '<h1>(no summary)</h1>')

    def test_a_very_long_summary_is_cut_for_display(self) -> None:
        long = 'x' * 5000
        found = signature(f'[{long}] {long}')
        self.assertEqual((len(found.title), len(found.subject), found.title[-1]), (128, 128, '…'))

        event = self.error(self.pinned(), long)
        for url in ('/errors/', '/errors/?view=list', f'/event/{event.id}/'):
            with self.subTest(url=url):
                self.assertNotIn('x' * 600, self.client.get(url).content.decode().replace(f'summary={long}', ''))
        (group,) = json.loads(self.client.get('/api/errors/').content)['groups']
        self.assertEqual(len(group['title']), 128)

    def test_a_binary_is_named_without_its_directory(self) -> None:
        self.assertEqual(signature('[Engines/A-1] Stalled during genfens').subject, 'A-1')
        self.assertEqual(signature('[Engines\\A-1] Bench Exceeded Max Duration').subject, 'A-1')
        self.assertEqual(signature('[A-1] Bench Exceeded Max Duration').subject, 'A-1')
        test = self.pinned()
        self.error(test, '[Engines/A-1] Bench Exceeded Max Duration')
        self.error(test, '[A-1] Bench Exceeded Max Duration')
        self.assertEqual([row.group.count for row in self.rows()], [2])


class GroupEventsLinkTests(TriageCase):
    def listed(self, querystring: str) -> list[str]:
        html = self.client.get(f'/errors/{querystring}').content.decode()
        return [cell.split('</td>', 1)[0] for cell in html.split('<td class="triage-summary">')[1:]]

    def test_the_count_links_to_exactly_the_groups_events(self) -> None:
        test, other = self.pinned(), self.pinned()
        for _ in range(14):
            self.error(test, 'Stalled')
        self.error(test, 'Disconnect')
        self.error(test, 'Disconnect')
        self.error(other, 'Stalled')
        self.error(test, wrong_bench_summary(test, 11))
        self.error(test, wrong_bench_summary(test, 12))

        rows = {(row.group.test_id, row.group.signature.title): row for row in self.rows()}
        html = self.client.get('/errors/').content.decode()

        for key, count in (((test.id, 'Stalled'), 14), ((test.id, 'Disconnect'), 2), ((test.id, 'Wrong Bench'), 2)):
            with self.subTest(key=key):
                row = rows[key]
                self.assertEqual(row.group.count, count)
                self.assertIn(f'href="/errors/{row.events_querystring.replace("&", "&amp;")}"', html)
                self.assertEqual(len(self.listed(row.events_querystring)), count)

        self.assertEqual(set(self.listed(rows[test.id, 'Stalled'].events_querystring)), {'Stalled'})

    def test_the_summary_filter_is_shown_and_can_be_dropped(self) -> None:
        test = self.pinned()
        self.error(test, 'Stalled')
        html = self.client.get(f'/errors/?workload={test.id}&summary=Stalled&view=list').content.decode()
        self.assertIn('Showing 1 chosen summary only.', html)
        self.assertIn(f'href="/errors/?workload={test.id}&amp;view=list">Show every summary</a>', html)

    def test_a_machine_reporting_two_numbers_is_one_registration(self) -> None:
        test = self.pinned()
        machine = self.batch_machine()
        self.error(test, wrong_bench_summary(test, 11), machine=machine)
        self.error(test, wrong_bench_summary(test, 12), machine=machine)
        (row,) = self.rows()
        self.assertEqual((row.group.count, row.affected.registrations, row.affected.hosts), (2, 1, 1))
