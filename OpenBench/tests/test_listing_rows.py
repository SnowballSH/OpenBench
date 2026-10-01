from dataclasses import dataclass, field
from datetime import timedelta
from html.parser import HTMLParser
from typing import Any, ClassVar

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from OpenBench.listing_rows import is_commit_name, listing_moment, short_name, workload_label
from OpenBench.models import LogEvent, Test, WorkloadSnapshot
from OpenBench.page_queries import listing_tests
from OpenBench.templatetags.mytags import prettyDevName, prettyName
from OpenBench.tests.datasets import SMALL, Dataset, build_dataset
from OpenBench.tests.fixtures import create_engine_config, create_test, create_user, ensure_book, present

DEV_SHA = '76f2da3c0b1e4f5a9d8c7b6a5e4f3d2c1b0a9f8e'
BASE_SHA = '8c308d43aa11bb22cc33dd44ee55ff6677889900'


@dataclass
class Row:
    href: str
    primary_links: list[str] = field(default_factory=list)


class RowAudit(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.rows: list[Row] = []
        self.stray_primary_links: list[str] = []
        self.open_row: Row | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == 'tr':
            href = values.get('data-row-href')
            self.open_row = Row(href) if href is not None else None
            if self.open_row:
                self.rows.append(self.open_row)
        elif tag == 'a' and 'row-link' in (values.get('class') or '').split():
            target = values.get('href') or ''
            if self.open_row:
                self.open_row.primary_links.append(target)
            else:
                self.stray_primary_links.append(target)

    def handle_endtag(self, tag: str) -> None:
        if tag == 'tr':
            self.open_row = None

    def defects(self) -> list[str]:
        mismatched = [row.href for row in self.rows if row.primary_links != [row.href]]
        return [f'row {href!r} needs exactly one matching primary link' for href in mismatched] + [
            f'primary link {href!r} outside a navigable row' for href in self.stray_primary_links
        ]


def row_audit(html: str) -> RowAudit:
    audit = RowAudit()
    audit.feed(html)
    audit.close()
    return audit


class RowAuditSelfTests(SimpleTestCase):
    def defects(self, row: str) -> list[str]:
        return row_audit(f'<table>{row}</table>').defects()

    def test_accepts_one_matching_primary_link_among_other_links(self) -> None:
        links = '<a href="/user/a">a</a><a class="row-link" href="/test/1/">x</a>'
        row = f'<tr data-row-href="/test/1/"><td>{links}</td></tr>'
        self.assertEqual(self.defects(row), [])
        self.assertEqual(self.defects('<tr><td><a href="/user/a">a</a></td></tr>'), [])

    def test_flags_a_row_without_a_primary_link(self) -> None:
        self.assertEqual(len(self.defects('<tr data-row-href="/test/1/"><td><a href="/test/1/">x</a></td></tr>')), 1)

    def test_flags_two_primary_links(self) -> None:
        link = '<a class="row-link" href="/test/1/">x</a>'
        self.assertEqual(len(self.defects(f'<tr data-row-href="/test/1/"><td>{link}{link}</td></tr>')), 1)

    def test_flags_a_primary_link_that_goes_elsewhere(self) -> None:
        row = '<tr data-row-href="/test/1/"><td><a class="row-link" href="/test/2/">x</a></td></tr>'
        self.assertEqual(len(self.defects(row)), 1)

    def test_flags_a_primary_link_in_a_plain_row(self) -> None:
        self.assertEqual(len(self.defects('<tr><td><a class="row-link" href="/test/1/">x</a></td></tr>')), 1)


class CommitNameTests(SimpleTestCase):
    def test_recognises_full_and_short_shas(self) -> None:
        for name in (DEV_SHA, DEV_SHA.upper(), DEV_SHA[:7], DEV_SHA[:12]):
            with self.subTest(name=name):
                self.assertTrue(is_commit_name(name))
                self.assertEqual(short_name(name), '76f2da3c'[: len(name)])

    def test_agrees_with_pretty_name_on_a_full_sha_without_digits(self) -> None:
        name = 'abcdef' * 6 + 'abcd'
        self.assertNotEqual(prettyName(name), name)
        self.assertTrue(is_commit_name(name))
        self.assertFalse(is_commit_name(name[:39]))

    def test_leaves_branch_names_alone(self) -> None:
        for name in ('master', 'lmr-tweak', 'deadbeef', 'defaced', '76f2da', DEV_SHA + '0', '1234567-fix', ''):
            with self.subTest(name=name):
                self.assertFalse(is_commit_name(name))
                self.assertEqual(short_name(name), name)


class WorkloadLabelTests(TestCase):
    def setUp(self) -> None:
        create_engine_config()
        create_engine_config('Other')
        ensure_book()
        self.author = create_user('author')

    def pinned(self, info: str = '', base: str = BASE_SHA, **fields: Any) -> Test:
        test = create_test(self.author, info=info, **fields)
        test.dev.name, test.base.name = DEV_SHA, base
        return test

    def label(self, test: Test) -> tuple[str, str | None, str]:
        label = workload_label(test, prettyDevName(test))
        return label.title, label.commits, label.info

    def test_a_branch_keeps_its_pretty_name(self) -> None:
        self.assertEqual(self.label(create_test(self.author, info='Some notes')), ('dev', None, 'Some notes'))

    def test_a_pinned_commit_is_titled_by_its_subject(self) -> None:
        test = self.pinned(' Scale LMR by history \n')
        self.assertEqual(self.label(test), ('Scale LMR by history', '76f2da3c vs 8c308d43', ''))

    def test_the_info_lines_after_the_subject_stay_in_the_info_column(self) -> None:
        test = self.pinned('Scale LMR by history, LTC confirmation of #5\navl:6b10ec947ac0\n\nBench: 123\n')
        self.assertEqual(
            self.label(test),
            ('Scale LMR by history, LTC confirmation of #5', '76f2da3c vs 8c308d43', 'avl:6b10ec947ac0\n\nBench: 123'),
        )

    def test_a_pinned_commit_without_info_shows_both_commits(self) -> None:
        self.assertEqual(self.label(self.pinned()), ('76f2da3c vs 8c308d43', None, ''))
        self.assertEqual(self.label(self.pinned(base='master')), ('76f2da3c vs master', None, ''))

    def test_a_commit_against_itself_is_named_once(self) -> None:
        self.assertEqual(self.label(self.pinned('Sanity run', base=DEV_SHA)), ('Sanity run', '76f2da3c', ''))

    def test_another_engine_or_a_network_still_names_the_row(self) -> None:
        self.assertEqual(
            self.label(self.pinned('Subject', base_engine='Other')), (f'[Other] {BASE_SHA}', None, 'Subject')
        )
        network_test = self.pinned(
            'Subject', base=DEV_SHA, dev_network='AAAAAAAA', base_network='BBBBBBBB', dev_netname='net-7'
        )
        self.assertEqual(self.label(network_test), ('net-7', None, 'Subject'))


class ListingMomentTests(TestCase):
    def setUp(self) -> None:
        create_engine_config()
        ensure_book()
        self.author = create_user('author')
        self.now = timezone.now()

    def moment(self, minutes_since_creation: int, snapshots: tuple[int, ...] = (), **fields: Any) -> tuple[str, str]:
        test = create_test(self.author, **fields)
        Test.objects.filter(id=test.id).update(
            creation=self.now - timedelta(minutes=minutes_since_creation), updated=self.now
        )
        for minutes in snapshots:
            snapshot = WorkloadSnapshot.objects.create(test=test, games=0)
            WorkloadSnapshot.objects.filter(id=snapshot.id).update(created=self.now - timedelta(minutes=minutes))
        moment = present(listing_moment(listing_tests(Test.objects.filter(id=test.id), self.now).get()))
        return moment.verb, moment.ago

    def test_a_finished_row_says_when_it_last_reported(self) -> None:
        self.assertEqual(self.moment(600, (500, 130), finished=True, passed=True), ('finished', '2h ago'))

    def test_a_running_row_says_when_it_started(self) -> None:
        self.assertEqual(self.moment(600, (45, 3)), ('started', '45m ago'))

    def test_rows_without_reports_say_when_they_were_created(self) -> None:
        self.assertEqual(self.moment(3 * 24 * 60, approved=False), ('created', '3d ago'))
        self.assertEqual(self.moment(7), ('created', '7m ago'))

    def test_a_bare_test_has_no_moment(self) -> None:
        test = create_test(self.author)
        with self.assertNumQueries(0):
            self.assertIsNone(listing_moment(test))


class RenderedRowTests(TestCase):
    data: ClassVar[Dataset]
    pinned: ClassVar[Test]

    @classmethod
    def setUpTestData(cls) -> None:
        cls.data = build_dataset(SMALL)
        cls.pinned = create_test(
            cls.data.users[0], info='Scale LMR by <history>\navl:76f2da3c0b1e', finished=True, passed=True
        )
        cls.pinned.dev.name, cls.pinned.base.name = DEV_SHA, BASE_SHA
        cls.pinned.dev.save()
        cls.pinned.base.save()
        machine = cls.data.machines[0]
        LogEvent.objects.create(
            author='user1', summary='Crash', log_file='', machine_id=machine.id, test_id=cls.pinned.id
        )

    def setUp(self) -> None:
        self.client.force_login(self.data.users[0])

    def audited(self, url: str) -> RowAudit:
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        audit = row_audit(response.content.decode())
        self.assertEqual(audit.defects(), [])
        return audit

    def test_every_listing_row_names_one_destination(self) -> None:
        machine = self.data.machines[-1]
        pages = {
            '/index/': '/test/',
            '/index/2/': '/test/',
            '/greens/': '/test/',
            f'/user/{self.data.users[0].username}/': '/test/',
            '/search/?keywords=branch': '/test/',
            '/machines/?show=7d': '/machines/',
            f'/machines/{machine.id}/': '/test/',
            '/users/': '/user/',
            '/events/': '/test/',
            '/errors/': '/test/',
            '/progress/?window=all': '/',
        }
        for url, prefix in pages.items():
            with self.subTest(url=url):
                rows = self.audited(url).rows
                self.assertTrue(rows, 'no navigable rows rendered')
                self.assertTrue(any(row.href.startswith(prefix) for row in rows))

    def test_workload_listings_make_every_workload_row_navigable(self) -> None:
        html = self.client.get('/index/').content.decode()
        self.assertEqual(html.count('<td class="row-name">'), len(row_audit(html).rows))

    def test_an_error_opens_its_log_or_else_its_workload(self) -> None:
        logged = LogEvent.objects.create(
            author='user1', summary='Crash', log_file='crash.log', machine_id=1, test_id=self.pinned.id
        )
        destinations = [row.href for row in self.audited('/errors/').rows]
        self.assertIn(f'/event/{logged.id}', destinations)
        self.assertIn(f'/test/{self.pinned.id}/', destinations)

    def test_networks_stay_plain_rows(self) -> None:
        self.assertEqual(self.audited('/networks/').rows, [])

    def test_a_pinned_commit_row_shows_its_number_subject_and_commits(self) -> None:
        html = self.client.get('/greens/').content.decode()
        row = html.split(f'<tr data-row-href="/test/{self.pinned.id}/">', 1)[1].split('</tr>', 1)[0]
        self.assertIn(f'<span class="row-id">#{self.pinned.id}</span>', row)
        self.assertIn('title="Scale LMR by &lt;history&gt;">', row)
        self.assertIn('</span> Scale LMR by &lt;history&gt;</a>', row)
        self.assertIn('<div class="row-meta mono">76f2da3c vs 8c308d43</div>', row)
        self.assertIn('<td class="test-info"><div title="avl:76f2da3c0b1e">avl:76f2da3c0b1e</div></td>', row)
        self.assertRegex(row, r'<time datetime="[^"]+" title="[^"]+">finished [^<]+</time>')

    def test_authors_are_named_in_full(self) -> None:
        author = self.data.users[0].username
        html = self.client.get('/greens/').content.decode()
        self.assertIn(f'title="{author.capitalize()}">{author.capitalize()}</a>', html)

    def test_the_index_offers_a_filter_wired_to_its_table(self) -> None:
        html = self.client.get('/index/').content.decode()
        self.assertIn('data-row-filter="workload-list" data-row-filter-count="row-filter-count"', html)
        self.assertIn('<table id="workload-list"', html)
        self.assertIn('id="row-filter-count"', html)
