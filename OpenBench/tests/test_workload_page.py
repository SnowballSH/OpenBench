import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any

from django.test import TestCase

from OpenBench.insights.domain import WorkloadStatus
from OpenBench.insights.results.verdict import VerdictKind
from OpenBench.insights.sources import workload_facts
from OpenBench.insights.workload import workload_insights
from OpenBench.models import Machine, SPSARun, Test, WorkloadSnapshot
from OpenBench.tests.fixtures import (
    create_engine_config,
    create_test,
    create_user,
    ensure_book,
    logged_build_failure,
    present,
    system_info,
)
from OpenBench.workloads.page import (
    PageSection,
    StateBadge,
    SummaryMeter,
    page_header,
    page_sections,
    page_verdict,
    state_table,
    summary_meter,
    workload_page,
)

SHA_DEV = '76f2da3c' + 'a' * 32
SHA_BASE = '8c308d43' + 'b' * 32

PLAYED: dict[str, Any] = {
    'games': 1000,
    'losses': 250,
    'draws': 450,
    'wins': 300,
    'LL': 20,
    'LD': 110,
    'DD': 220,
    'DW': 120,
    'WW': 30,
    'currentllr': 1.47,
}

SCRIPT_HOOKS = (
    'data-workload-insights',
    'data-insights-evidence',
    'data-insights-status',
    'data-insights-error',
    'data-insights-announcer',
    'data-insights-verdict',
    'data-insights-forecast',
    'data-verdict-label',
    'data-verdict-figures',
    'data-verdict-text',
    'data-summary-meter',
    'data-summary-timing',
    'data-insights-tiles',
    'data-insights-charts',
    'data-insights-results',
    'data-insights-contributions',
    'data-insights-workers-note',
    'data-live-badge',
    'data-live-outcome',
    'data-workload-announcer',
    'id="long-statblock"',
    'id="summary-container"',
    'id="results-container"',
    'data-workload-action="fetch-results"',
    'data-workload-action="copy-statblock"',
)

SITE_LOGOUT = '/logout/'

LIVE_HOOKS = ('data-live-workload', 'data-live-indicator', 'data-live-notify ', 'data-live-notify-note')


@dataclass
class PageOutline:
    headings: list[tuple[int, str]] = field(default_factory=list)
    sections: list[tuple[str, bool]] = field(default_factory=list)
    links: list[tuple[str, bool]] = field(default_factory=list)
    ids: set[str] = field(default_factory=set)
    nav_labels: list[str] = field(default_factory=list)
    action_owners: list[str] = field(default_factory=list)
    captionless_tables: int = 0


class OutlineParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.outline = PageOutline()
        self.open_sections: list[str] = []
        self.heading: int | None = None
        self.table_captions: list[bool] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name: value or '' for name, value in attrs}
        if 'id' in values:
            self.outline.ids.add(values['id'])
        if tag == 'section':
            self.open_sections.append(values.get('id', ''))
            if 'data-section' in values:
                self.outline.sections.append((values['data-section'], 'hidden' in values))
        if 'data-section-link' in values:
            self.outline.links.append((values['href'], 'hidden' in values))
        if tag == 'nav':
            self.outline.nav_labels.append(values.get('aria-label', ''))
        posts = tag == 'form' and values.get('method', '').lower() == 'post' and values.get('action') != SITE_LOGOUT
        if 'formaction' in values or posts:
            self.outline.action_owners.append(self.open_sections[-1] if self.open_sections else '')
        if re.fullmatch(r'h[1-6]', tag):
            self.heading = int(tag[1])
            self.outline.headings.append((self.heading, ''))
        if tag == 'table':
            self.table_captions.append(False)
        if tag == 'caption' and self.table_captions:
            self.table_captions[-1] = True

    def handle_endtag(self, tag: str) -> None:
        if tag == 'section' and self.open_sections:
            self.open_sections.pop()
        if re.fullmatch(r'h[1-6]', tag):
            self.heading = None
        if tag == 'table' and self.table_captions and not self.table_captions.pop():
            self.outline.captionless_tables += 1

    def handle_data(self, data: str) -> None:
        if self.heading is not None:
            level, text = self.outline.headings[-1]
            self.outline.headings[-1] = (level, text + data)


def outline_of(html: str) -> PageOutline:
    parser = OutlineParser()
    parser.feed(html)
    parser.close()
    return parser.outline


class WorkloadPageCase(TestCase):
    def setUp(self) -> None:
        create_engine_config()
        ensure_book()
        self.author = create_user('author', approver=True)
        self.client.force_login(self.author)

    def tune(self, **fields: Any) -> Test:
        test = create_test(self.author, test_mode='SPSA', workload_size=8, **fields)
        SPSARun.objects.create(
            tune=test,
            reporting_type='BATCHED',
            distribution_type='SINGLE',
            alpha=0.602,
            gamma=0.101,
            iterations=100,
            pairs_per=8,
            a_ratio=0.1,
        )
        return test

    def pinned(self, info: str, **fields: Any) -> Test:
        test = create_test(self.author, info=info, **fields)
        for engine, sha in ((test.dev, SHA_DEV), (test.base, SHA_BASE)):
            engine.name = engine.sha = sha
            engine.save()
        return test

    def html(self, test: Test) -> str:
        response = self.client.get(f'/{test.workload_type_str()}/{test.id}/')
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def outline(self, test: Test) -> PageOutline:
        return outline_of(self.html(test))


class PageHeaderTests(WorkloadPageCase):
    def header(self, test: Test):
        return page_header(test, workload_facts(test))

    def test_a_branch_test_is_titled_by_its_dev_branch(self) -> None:
        header = self.header(create_test(self.author, info='Try a wider window'))
        self.assertEqual((header.title, header.pair, header.details), ('dev', 'dev vs base', 'Try a wider window'))

    def test_a_commit_pinned_test_is_titled_by_its_subject(self) -> None:
        header = self.header(self.pinned('Pawn correction history, LTC confirmation of #5\navl:6b10ec947ac0'))
        self.assertEqual(header.title, 'Pawn correction history, LTC confirmation of #5')
        self.assertEqual(header.pair, '76f2da3c vs 8c308d43')
        self.assertEqual(header.details, 'avl:6b10ec947ac0')

    def test_states_what_kind_of_run_it_is(self) -> None:
        cases: tuple[tuple[dict[str, Any], str | None, str, str], ...] = (
            ({}, 'STC', '8.0+0.08', 'SPRT [0, 3]'),
            ({'dev_time_control': '40.0+0.4', 'base_time_control': '40.0+0.4'}, 'LTC', '40.0+0.4', 'SPRT [0, 3]'),
            ({'test_mode': 'GAMES', 'max_games': 4000}, 'STC', '8.0+0.08', 'Fixed 4,000 games'),
            ({'test_mode': 'DATAGEN', 'max_games': 5000}, 'STC', '8.0+0.08', 'Datagen, 5,000 games'),
            ({'base_time_control': '10.0+0.1'}, None, '8.0+0.08 vs 10.0+0.1', 'SPRT [0, 3]'),
        )
        for fields, time_class, time_control, mode in cases:
            with self.subTest(fields=fields):
                header = self.header(create_test(self.author, **fields))
                self.assertEqual(
                    (header.time_class, header.time_control, header.mode), (time_class, time_control, mode)
                )
        self.assertEqual(self.header(self.tune()).mode, 'SPSA tune')

    def test_names_both_engines_only_when_they_differ(self) -> None:
        self.assertEqual(self.header(create_test(self.author)).engine, 'Avalanche')
        self.assertEqual(self.header(create_test(self.author, base_engine='Other')).engine, 'Avalanche vs Other')

    def test_state_badge_per_state(self) -> None:
        cases: tuple[tuple[dict[str, Any], StateBadge], ...] = (
            ({'approved': False}, StateBadge('Pending approval', 'warn')),
            ({}, StateBadge('Running', 'accent')),
            ({'finished': True, 'passed': True}, StateBadge('Passed', 'pass')),
            ({'finished': True, 'failed': True, 'losses': 5}, StateBadge('Failed', 'fail')),
            ({'finished': True, 'failed': True}, StateBadge('Failed, wins at least losses', 'fail')),
            ({'finished': True}, StateBadge('Stopped', 'neutral')),
            ({'finished': True, 'passed': True, 'deleted': True}, StateBadge('Deleted', 'neutral')),
        )
        for fields, badge in cases:
            with self.subTest(fields=fields):
                self.assertEqual(self.header(create_test(self.author, **fields)).state, badge)

    def test_a_fixed_run_completes_instead_of_passing_or_failing(self) -> None:
        for mode in ('GAMES', 'DATAGEN'):
            for flag in ('passed', 'failed'):
                with self.subTest(mode=mode, flag=flag):
                    fields: dict[str, Any] = {'test_mode': mode, 'max_games': 1000, 'finished': True, flag: True}
                    test = create_test(self.author, **fields)
                    self.assertEqual(self.header(test).state, StateBadge('Completed', 'neutral'))

    def test_the_state_table_is_what_the_live_script_draws(self) -> None:
        sprt = state_table(workload_facts(create_test(self.author)))
        self.assertEqual(set(sprt), {status.value for status in WorkloadStatus})
        self.assertEqual(sprt['passed'], {'label': None, 'variant': 'pass'})
        self.assertEqual(sprt['stopped'], {'label': 'Stopped', 'variant': 'neutral'})

        fixed = state_table(workload_facts(create_test(self.author, test_mode='GAMES', max_games=1000)))
        self.assertEqual(fixed['passed'], {'label': 'Completed', 'variant': 'neutral'})
        self.assertEqual(fixed['failed'], fixed['passed'])
        self.assertEqual(fixed['active'], sprt['active'])

    def test_header_runs_no_queries(self) -> None:
        test = Test.objects.select_related('dev', 'base').get(id=create_test(self.author).id)
        with self.assertNumQueries(0):
            page_header(test, workload_facts(test))


class PageSummaryTests(WorkloadPageCase):
    def test_a_running_sprt_meter_is_the_llr_position(self) -> None:
        meter = present(summary_meter(workload_facts(create_test(self.author, currentllr=1.47))))
        self.assertEqual(
            (meter.kind, meter.caption, meter.label), ('position', 'LLR', 'LLR 1.47 between -2.94 and 2.94')
        )
        self.assertAlmostEqual(meter.fraction, 0.75)

    def test_a_running_fixed_run_meter_is_games_over_the_target(self) -> None:
        for mode in ('GAMES', 'DATAGEN'):
            test = create_test(self.author, test_mode=mode, max_games=4000, **{**PLAYED, 'games': 1000})
            self.assertEqual(
                summary_meter(workload_facts(test)), SummaryMeter('fill', 'Games', 0.25, '1,000 of 4,000 games')
            )

    def test_a_running_tune_meter_is_games_over_its_iterations(self) -> None:
        test = self.tune(games=400, losses=100, draws=200, wins=100)
        self.assertEqual(summary_meter(workload_facts(test)), SummaryMeter('fill', 'Games', 0.25, '400 of 1,600 games'))

    def test_the_meter_is_clamped(self) -> None:
        for llr, fraction in ((9.0, 1.0), (-9.0, 0.0)):
            meter = present(summary_meter(workload_facts(create_test(self.author, currentllr=llr))))
            self.assertEqual(meter.fraction, fraction)

    def test_only_a_running_workload_has_a_meter(self) -> None:
        settled: tuple[dict[str, Any], ...] = (
            {'approved': False},
            {'finished': True, 'passed': True},
            {'finished': True},
            {'deleted': True},
        )
        for fields in settled:
            with self.subTest(fields=fields):
                self.assertIsNone(summary_meter(workload_facts(create_test(self.author, **fields))))

    def test_the_verdict_is_the_insights_verdict(self) -> None:
        test = create_test(self.author, **PLAYED)
        running = present(page_verdict(workload_facts(test)))
        self.assertEqual(running.kind, VerdictKind.LIKELY_GAIN)
        self.assertEqual(running, present(workload_insights(test).results).verdict)

        passed = present(page_verdict(workload_facts(create_test(self.author, finished=True, passed=True, **PLAYED))))
        self.assertEqual(passed.kind, VerdictKind.PASSED)

    def test_no_verdict_without_games_or_for_a_tune(self) -> None:
        self.assertIsNone(page_verdict(workload_facts(create_test(self.author))))
        self.assertIsNone(page_verdict(workload_facts(self.tune(games=400, losses=100, draws=200, wins=100))))

    def test_timing_costs_one_query_and_only_once_games_were_played(self) -> None:
        idle = create_test(self.author)
        played = create_test(self.author, **PLAYED)
        for index in range(40):
            WorkloadSnapshot.objects.create(test=played, games=25 * index, llr=0.0)

        with self.assertNumQueries(0):
            timing = present(workload_page(idle, 0).summary.timing)
        self.assertEqual((timing.kind, timing.text, timing.rate), ('unavailable', 'needs 200 games first', None))
        with self.assertNumQueries(1):
            workload_page(played, 0)

    def test_nothing_is_timed_before_games_unless_the_workload_is_running(self) -> None:
        idle: tuple[dict[str, Any], ...] = ({'approved': False}, {'finished': True})
        for fields in idle:
            test = create_test(self.author, **fields)
            with self.subTest(fields=fields), self.assertNumQueries(0):
                self.assertIsNone(workload_page(test, 0).summary.timing)

    def test_a_finished_workload_says_how_long_it_took(self) -> None:
        test = create_test(self.author, finished=True, passed=True, **PLAYED)
        self.assertRegex(present(present(workload_page(test, 0).summary.timing).text), r'^took ')


class PageSectionsTests(WorkloadPageCase):
    def listed(self, test: Test, errors: int = 0) -> list[tuple[str, bool]]:
        return [(section.id, section.shown) for section in page_sections(test, workload_facts(test), errors).listed()]

    def test_evidence_comes_before_reference_and_waits_for_games(self) -> None:
        self.assertEqual(
            self.listed(create_test(self.author)),
            [
                ('results', False),
                ('progress', False),
                ('workers', False),
                ('configuration', True),
                ('raw-results', True),
            ],
        )
        self.assertEqual(
            self.listed(create_test(self.author, **PLAYED)),
            [('results', True), ('progress', True), ('workers', True), ('configuration', True), ('raw-results', True)],
        )

    def test_games_parameters_and_errors_exist_only_when_they_can_show_something(self) -> None:
        uploading = self.listed(create_test(self.author, upload_pgns='COMPACT', **PLAYED), errors=2)
        self.assertEqual(
            [name for name, _ in uploading],
            ['results', 'progress', 'workers', 'games', 'errors', 'configuration', 'raw-results'],
        )
        self.assertIn(('games', False), uploading)

        tune = self.listed(self.tune(games=400, losses=100, draws=200, wins=100))
        self.assertEqual(
            [name for name, _ in tune], ['progress', 'workers', 'parameters', 'configuration', 'raw-results']
        )

    def test_the_errors_entry_carries_the_count(self) -> None:
        test = create_test(self.author)
        sections = page_sections(test, workload_facts(test), 3)
        self.assertEqual(sections.errors, PageSection('errors', 'Errors (3)', True))


class RenderedWorkloadPageTests(WorkloadPageCase):
    def states(self) -> dict[str, Test]:
        return {
            'pending': create_test(self.author, approved=False),
            'no games yet': create_test(self.author),
            'running': create_test(self.author, **PLAYED),
            'passed': create_test(self.author, finished=True, passed=True, **PLAYED),
            'failed': create_test(self.author, finished=True, failed=True, **PLAYED),
            'stopped': create_test(self.author, finished=True),
            'deleted': create_test(self.author, deleted=True, **PLAYED),
            'fixed games': create_test(self.author, test_mode='GAMES', max_games=4000, **PLAYED),
            'uploading': create_test(self.author, upload_pgns='COMPACT', **PLAYED),
            'datagen': create_test(self.author, test_mode='DATAGEN', max_games=4000, **PLAYED),
            'tune': self.tune(games=400, losses=100, draws=200, wins=100),
        }

    def test_one_visible_title_then_sections_in_order(self) -> None:
        for state, test in self.states().items():
            with self.subTest(state=state):
                outline = self.outline(test)
                level_one = [text.strip() for level, text in outline.headings if level == 1]
                self.assertEqual(level_one, [f'#{test.id} dev'])
                self.assertNotIn('class="visually-hidden">#', self.html(test))

                facts = workload_facts(test)
                expected = [(section.id, not section.shown) for section in page_sections(test, facts, 0).listed()]
                self.assertEqual(outline.sections, expected)
                self.assertEqual(outline.links, [(f'#{name}', hidden) for name, hidden in expected])

    def test_every_nav_entry_has_its_section_and_the_nav_is_labelled(self) -> None:
        for state, test in self.states().items():
            with self.subTest(state=state):
                outline = self.outline(test)
                self.assertTrue({href.removeprefix('#') for href, _ in outline.links} <= outline.ids)
                self.assertEqual(outline.nav_labels, ['Site', 'Page sections'])
                self.assertEqual(outline.captionless_tables, 0)

    def test_sections_per_state(self) -> None:
        shown = {
            state: [name for name, hidden in self.outline(test).sections if not hidden]
            for state, test in self.states().items()
        }
        reference = ['configuration', 'raw-results']
        evidence = ['results', 'progress', 'workers']
        self.assertEqual(shown['pending'], reference)
        self.assertEqual(shown['no games yet'], reference)
        self.assertEqual(shown['stopped'], reference)
        for state in ('running', 'passed', 'failed', 'deleted', 'fixed games', 'uploading', 'datagen'):
            self.assertEqual(shown[state], evidence + reference, state)
        self.assertEqual(shown['tune'], ['progress', 'workers', 'parameters', *reference])

    def test_the_errors_section_appears_with_errors(self) -> None:
        test = create_test(self.author)
        self.assertNotIn(('errors', False), self.outline(test).sections)

        machine = Machine.objects.create(user=self.author, info={**system_info(), 'supported': ['Avalanche']})
        logged_build_failure(self, test, machine)
        outline = self.outline(test)
        self.assertIn(('errors', False), outline.sections)
        self.assertIn(('#errors', False), outline.links)
        self.assertIn('data-section-link="errors">Errors (1)</a>', self.html(test))

    def test_the_header_states_what_the_workload_is(self) -> None:
        test = self.pinned('Pawn correction history\navl:6b10ec947ac0', finished=True, passed=True, **PLAYED)
        content = self.html(test)
        self.assertIn(f'<h1><span class="row-id">#{test.id}</span> Pawn correction history</h1>', content)
        self.assertIn('<span class="badge badge-pass" data-live-badge>Passed</span>', content)
        self.assertIn('>76f2da3c vs 8c308d43<', content)
        self.assertIn('<li>STC <span class="mono">8.0+0.08</span></li>', content)
        self.assertIn('<li>SPRT [0, 3]</li>', content)
        self.assertIn('<p class="workload-details">avl:6b10ec947ac0</p>', content)

    def test_the_verdict_is_rendered_once_by_the_server(self) -> None:
        content = self.html(create_test(self.author, finished=True, passed=True, **PLAYED))
        self.assertEqual(content.count('data-insights-verdict'), 1)
        self.assertIn('class="status-verdict status-verdict-positive" data-insights-verdict>', content)
        self.assertIn('<p class="status-label" data-verdict-label>Passed</p>', content)
        self.assertIn('<p data-verdict-text>Passed: after', content)

    def test_the_status_card_states_the_verdict_in_figures_not_a_sentence(self) -> None:
        content = self.html(create_test(self.author, **PLAYED))
        card = content.split('class="card status-card"', 1)[1].split('</section>', 1)[0]
        visible = card.split('<details class="status-explain">', 1)[0]
        self.assertIn('<p class="status-label" data-verdict-label>Likely a gain</p>', visible)
        self.assertEqual(re.findall(r'<dt>([^<]+)</dt>', visible), ['Elo', 'LOS'])
        self.assertNotIn('Likely a gain:', visible)
        self.assertIn('<div class="status-forecast" data-insights-forecast></div>', card)

    def test_the_worker_status_is_one_line_with_its_explanation_folded(self) -> None:
        content = self.html(create_test(self.author, **PLAYED))
        card = content.split('class="card status-card"', 1)[1].split('</section>', 1)[0]
        self.assertRegex(card, r'<p class="diagnosis-brief">[A-Z][^<]{3,60}</p>')
        self.assertIn('<details class="diagnosis-details">', card)
        self.assertNotRegex(content, r'<li>\s*</li>')

    def test_the_summary_holds_numbers_only_and_the_cards_sit_beside_it(self) -> None:
        content = self.html(create_test(self.author, **PLAYED))
        summary = content.split('class="workload-summary"', 1)[1].split('</section>', 1)[0]
        self.assertNotIn('diagnosis', summary)
        self.assertNotIn('verdict', summary)
        side = content.split('<div class="workload-side">', 1)[1]
        self.assertLess(side.index('id="status-title"'), side.index('id="actions"'))

    def test_a_settled_workload_with_nothing_to_judge_has_no_status_card(self) -> None:
        self.assertNotIn('status-card', self.html(create_test(self.author, finished=True)))
        self.assertIn('status-card', self.html(create_test(self.author, approved=False)))

    def test_no_verdict_is_shown_before_games(self) -> None:
        self.assertIn('class="status-verdict" data-insights-verdict hidden>', self.html(create_test(self.author)))

    def test_a_running_workload_shows_its_meter_and_time_left(self) -> None:
        test = create_test(self.author, **PLAYED)
        content = self.html(test)
        self.assertIn('class="insight-meter insight-meter-position" role="meter"', content)
        self.assertIn('aria-valuetext="LLR 1.47 between -2.94 and 2.94"', content)
        self.assertIn('data-fraction="0.7500"', content)

        settled = self.html(create_test(self.author, finished=True, passed=True, **PLAYED))
        self.assertIn('<div class="summary-meter" data-summary-meter></div>', settled)
        self.assertIn('took ', settled)

    def test_every_state_changing_control_is_in_the_actions_section(self) -> None:
        for state, test in self.states().items():
            with self.subTest(state=state):
                owners = self.outline(test).action_owners
                self.assertTrue(owners)
                self.assertEqual(set(owners), {'actions'})

    def test_actions_offered_per_state(self) -> None:
        states = self.states()
        offered = {
            state: re.findall(r'formaction="/\w+/\d+/(\w+)/"', self.html(test)) for state, test in states.items()
        }
        self.assertEqual(offered['pending'], ['STOP', 'DELETE'])
        self.assertEqual(offered['running'], ['STOP', 'DELETE'])
        self.assertEqual(offered['passed'], ['DELETE'])
        self.assertEqual(offered['stopped'], ['RESTART', 'DELETE'])
        self.assertEqual(offered['deleted'], ['RESTORE', 'STOP'])
        self.assertIn('<a class="anchorbutton btn-disabled">Approve</a>', self.html(states['pending']))
        self.assertNotIn('>Approve<', self.html(states['running']))

    def test_another_approver_may_approve(self) -> None:
        test = create_test(create_user('someone'), approved=False)
        self.assertIn(f'formaction="/test/{test.id}/APPROVE/">Approve</button>', self.html(test))

    def test_destructive_actions_are_set_apart(self) -> None:
        content = self.html(create_test(self.author, **PLAYED))
        apart = content.split('<div class="action-row action-danger">', 1)[1].split('</div>', 1)[0]
        self.assertEqual(re.findall(r'/(\w+)/"', apart), ['STOP', 'DELETE'])
        self.assertNotIn('Clone', apart)

    def test_the_live_script_reads_its_states_from_the_page(self) -> None:
        test = create_test(self.author, test_mode='GAMES', max_games=4000, **PLAYED)
        island = self.html(test).split('<script id="workload-states" type="application/json">', 1)[1]
        self.assertEqual(json.loads(island.split('</script>', 1)[0]), state_table(workload_facts(test)))

    def test_the_meter_is_captioned(self) -> None:
        content = self.html(create_test(self.author, **PLAYED))
        self.assertIn('data-summary-meter><span class="summary-meter-caption">LLR</span><span', content)

    def test_a_running_workload_without_games_says_why_there_is_no_time_left(self) -> None:
        content = self.html(create_test(self.author))
        self.assertIn('class="row-timing-unavailable"', content)
        self.assertIn('needs 200 games first', content)

    def test_script_hooks_are_present(self) -> None:
        content = self.html(create_test(self.author, **PLAYED))
        for hook in (*SCRIPT_HOOKS, *LIVE_HOOKS):
            with self.subTest(hook=hook):
                self.assertEqual(content.count(hook), 1)

    def test_a_finished_workload_keeps_the_hooks_but_not_the_live_ones(self) -> None:
        content = self.html(create_test(self.author, finished=True, passed=True, **PLAYED))
        for hook in SCRIPT_HOOKS:
            with self.subTest(hook=hook):
                self.assertEqual(content.count(hook), 1)
        for hook in LIVE_HOOKS:
            with self.subTest(hook=hook):
                self.assertNotIn(hook, content)

    def test_the_configuration_is_complete(self) -> None:
        content = self.html(create_test(self.author, **PLAYED))
        configuration = content.split('id="configuration"', 1)[1].split('</section>', 1)[0]
        for caption in ('Dev', 'Base', 'Match'):
            self.assertIn(f'<caption class="contribution-caption">{caption}</caption>', configuration)
        for label in (
            'Engine',
            'Source',
            'Sha',
            'Branch',
            'Bench',
            'Network',
            'Options',
            'Time',
            'SPRT bounds',
            'Opening Book',
            'Upload PGNs',
            'Syzygy WDL',
            'Syzygy ADJ.',
            'Win ADJ.',
            'Draw ADJ.',
            'Scaling',
        ):
            with self.subTest(label=label):
                self.assertIn(f'class="td-label">{label}</th>', configuration)
        self.assertEqual(configuration.count('class="td-label">Sha</th>'), 2)
        self.assertIn('<td class="mono">[0.00, 3.00]</td>', configuration)

    def test_a_tune_has_one_engine_table_and_its_spsa_settings(self) -> None:
        content = self.html(self.tune())
        configuration = content.split('id="configuration"', 1)[1].split('</section>', 1)[0]
        self.assertIn('<caption class="contribution-caption">Engine</caption>', configuration)
        self.assertNotIn('<caption class="contribution-caption">Base</caption>', configuration)
        for label in ('Alpha', 'Gamma', 'A-Ratio', 'Reporting', 'Distribution', 'Pairs Per Point'):
            self.assertIn(f'class="td-label">{label}</th>', configuration)
        self.assertIn('data-workload-action="copy-spsa-inputs"', content)
        self.assertIn('data-workload-action="show-spsa-digest"', content)
        self.assertNotIn('data-workload-action="copy-statblock"', content)
