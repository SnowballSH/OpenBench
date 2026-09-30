from collections import Counter
from dataclasses import dataclass, field
from html.parser import HTMLParser
from unittest import mock

from django.test import TestCase

from OpenBench.config import OPENBENCH_CONFIG
from OpenBench.models import LogEvent, Machine, Network, Profile, SPSARun
from OpenBench.tests.fixtures import (
    create_engine_config,
    create_test,
    create_user,
    ensure_book,
    system_info,
)

VOID_TAGS = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
)
UNLABELLED_INPUT_TYPES = frozenset({"hidden", "submit", "button", "reset", "image"})
FORM_CONTROLS = frozenset({"input", "select", "textarea"})
NAMED_ELEMENTS = frozenset({"a", "button", "th"})


@dataclass
class Element:
    tag: str
    attrs: dict[str, str]
    parent: "Element | None"
    text: list[str] = field(default_factory=list)
    label_text: list[str] = field(default_factory=list)

    def hidden_from_assistive_tech(self) -> bool:
        node: Element | None = self
        while node is not None:
            if node.attrs.get("aria-hidden") == "true":
                return True
            node = node.parent
        return False

    def ancestor(self, tag: str) -> "Element | None":
        node = self.parent
        while node is not None and node.tag != tag:
            node = node.parent
        return node

    def accessible_name(self) -> str:
        return (self.attrs.get("aria-label") or "".join(self.text)).strip()


class AccessibilityAudit(HTMLParser):
    """Collects the page facts the checks below need from rendered HTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[Element] = []
        self.ids: Counter[str] = Counter()
        self.label_targets: set[str] = set()
        self.controls: list[Element] = []
        self.named: list[Element] = []
        self.headings: list[int] = []
        self.landmarks: Counter[str] = Counter()
        self.html_lang = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name: value or "" for name, value in attrs}
        element = Element(tag, values, self.stack[-1] if self.stack else None)

        if "id" in values:
            self.ids[values["id"]] += 1
        if tag == "html":
            self.html_lang = values.get("lang", "")
        if tag == "label" and "for" in values:
            self.label_targets.add(values["for"])
        if tag in {"main", "nav", "header"}:
            self.landmarks[tag] += 1
        if len(tag) == 2 and tag[0] == "h" and tag[1].isdigit():
            self.headings.append(int(tag[1]))
        if tag in FORM_CONTROLS and values.get("type", "").lower() not in UNLABELLED_INPUT_TYPES:
            self.controls.append(element)
        if tag in NAMED_ELEMENTS and not element.hidden_from_assistive_tech():
            self.named.append(element)

        if tag not in VOID_TAGS:
            self.stack.append(element)

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data: str) -> None:
        for element in self.stack:
            if not self.data_hidden_below(element):
                element.text.append(data)

    def data_hidden_below(self, element: Element) -> bool:
        below = self.stack[self.stack.index(element) + 1:]
        return any(node.attrs.get("aria-hidden") == "true" for node in below)

    def unlabelled_controls(self) -> list[str]:
        return [
            f"<{control.tag} id={control.attrs.get('id', '?')} name={control.attrs.get('name', '?')}>"
            for control in self.controls
            if not (
                control.attrs.get("aria-label", "").strip()
                or control.attrs.get("aria-labelledby", "").strip()
                or control.attrs.get("id") in self.label_targets
                or self.wrapping_label_has_text(control)
            )
        ]

    @staticmethod
    def wrapping_label_has_text(control: Element) -> bool:
        label = control.ancestor("label")
        return label is not None and bool("".join(label.text).strip())

    def nameless(self) -> list[str]:
        findings = []
        for element in self.named:
            if element.tag == "a" and "href" not in element.attrs:
                continue
            if not element.accessible_name():
                findings.append(f"<{element.tag} {element.attrs}>")
        return findings

    def skipped_heading_levels(self) -> list[str]:
        previous = 0
        skips = []
        for level in self.headings:
            if level > previous + 1:
                skips.append(f"h{previous} -> h{level}")
            previous = level
        return skips


def audit(html: str) -> AccessibilityAudit:
    parser = AccessibilityAudit()
    parser.feed(html)
    parser.close()
    return parser


class RenderedPageAccessibilityTests(TestCase):
    def setUp(self) -> None:
        create_engine_config()
        self.book = ensure_book()
        user = create_user("admin", approver=True)
        user.is_superuser = user.is_staff = True
        user.save()
        Profile.objects.filter(user=user).update(
            repos={"Avalanche": "https://github.com/SnowballSH/Avalanche"}, engine="Avalanche"
        )
        self.client.force_login(user)

        self.test = create_test(user, dev_options="Threads=1 Hash=16 UCI_Chess960=false")
        self.tune = create_test(user, test_mode="SPSA", workload_size=8)
        SPSARun.objects.create(
            tune=self.tune,
            reporting_type="BATCHED",
            distribution_type="SINGLE",
            alpha=0.602,
            gamma=0.101,
            iterations=100,
            pairs_per=8,
            a_ratio=0.1,
        )
        self.datagen = create_test(user, test_mode="DATAGEN")
        self.machine = Machine.objects.create(user=user, info={**system_info(), "supported": ["Avalanche"]})
        Network.objects.create(sha256="ABCDEF01", name="r1", engine="Avalanche", author="admin", default=True)
        Network.objects.create(sha256="ABCDEF02", name="r2", engine="Avalanche", author="admin", was_default=True)
        Network.objects.create(sha256="ABCDEF03", name="r3", engine="Avalanche", author="admin")
        LogEvent.objects.create(author="admin", summary="Approved", log_file="", test_id=self.test.id)

    def pages(self) -> list[str]:
        return [
            "/index/",
            "/greens/",
            "/user/admin/",
            "/search/",
            "/search/?go=1&keywords=dev",
            f"/test/{self.test.id}/",
            f"/tune/{self.tune.id}/",
            f"/datagen/{self.datagen.id}/",
            "/test/new/",
            f"/test/new/?clone={self.test.id}",
            "/tune/new/",
            "/datagen/new/",
            "/machines/",
            f"/machines/{self.machine.id}/",
            "/users/",
            "/events/",
            "/errors/",
            "/networks/",
            "/networks/Avalanche/",
            "/networks/Avalanche/EDIT/ABCDEF01/",
            "/newNetwork/",
            "/profile/",
            "/manage/books/",
            f"/manage/books/{self.book.name}/",
            "/manage/engines/",
            "/manage/engines/Avalanche/",
            "/manage/storage/",
            "/progress/",
        ]

    def assert_accessible(self, page: str) -> None:
        response = self.client.get(page)
        self.assertEqual(response.status_code, 200)
        result = audit(response.content.decode())

        self.assertEqual(result.html_lang, "en")
        self.assertEqual(result.unlabelled_controls(), [], "form controls without a label")
        self.assertEqual(result.nameless(), [], "links, buttons or headers without an accessible name")
        self.assertEqual([name for name, count in result.ids.items() if count > 1], [], "duplicate ids")
        self.assertEqual(result.landmarks["main"], 1, "one main landmark")
        self.assertGreaterEqual(result.landmarks["nav"], 1, "a navigation landmark")
        self.assertEqual(result.headings.count(1), 1, "one level-one heading")
        self.assertEqual(result.skipped_heading_levels(), [], "heading levels skipped")

    def test_pages_are_accessible(self) -> None:
        for page in self.pages():
            with self.subTest(page=page):
                self.assert_accessible(page)

    def test_login_is_accessible(self) -> None:
        self.client.logout()
        self.assert_accessible("/login/")

    def test_register_is_accessible(self) -> None:
        self.client.logout()
        with mock.patch.dict(OPENBENCH_CONFIG, {"require_manual_registration": False}):
            self.assert_accessible("/register/")

    def test_pages_carry_a_skip_link_and_distinct_titles(self) -> None:
        titles = set()
        for page in ["/index/", "/machines/", "/networks/", "/profile/", "/test/new/"]:
            html = self.client.get(page).content.decode()
            self.assertIn('<a class="skip-link" href="#content">', html)
            titles.add(html.split("<title>", 1)[1].split("</title>", 1)[0])
        self.assertEqual(len(titles), 5)


class AuditSelfTests(TestCase):
    def test_flags_an_unlabelled_control(self) -> None:
        self.assertEqual(len(audit('<label>Name</label><input name="x">').unlabelled_controls()), 1)
        self.assertEqual(audit('<label for="x">Name</label><input id="x">').unlabelled_controls(), [])
        self.assertEqual(audit('<label>Name <input name="x"></label>').unlabelled_controls(), [])
        self.assertEqual(audit('<input type="hidden" name="x">').unlabelled_controls(), [])

    def test_flags_an_icon_only_link(self) -> None:
        self.assertEqual(len(audit('<a href="/x"><i class="fa" aria-hidden="true"></i></a>').nameless()), 1)
        self.assertEqual(audit('<a href="/x" aria-label="Edit"><i class="fa"></i></a>').nameless(), [])
        self.assertEqual(len(audit("<table><tr><th></th></tr></table>").nameless()), 1)

    def test_flags_a_skipped_heading_level(self) -> None:
        self.assertEqual(audit("<h1>a</h1><h3>b</h3>").skipped_heading_levels(), ["h1 -> h3"])
        self.assertEqual(audit("<h1>a</h1><h2>b</h2><h3>c</h3><h2>d</h2>").skipped_heading_levels(), [])
