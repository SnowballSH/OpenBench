import re
from html.parser import HTMLParser
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.http import HttpResponse
from django.middleware.csrf import REASON_NO_CSRF_COOKIE
from django.test import Client, RequestFactory, TestCase, override_settings

from OpenBench.config import OPENBENCH_CONFIG
from OpenBench.models import Machine, Network, SPSARun
from OpenBench.security.csp import (
    HEADER,
    ContentSecurityPolicyMiddleware,
    serialize_policy,
)
from OpenBench.tests.fixtures import (
    create_engine_config,
    create_test,
    create_user,
    ensure_book,
    system_info,
)

ROOT = Path(settings.BASE_DIR)
TEMPLATES = sorted((ROOT / "Templates").rglob("*.html"))
SCRIPTS = sorted((ROOT / "OpenBench" / "static").glob("*.js"))
MYTAGS = ROOT / "OpenBench" / "templatetags" / "mytags.py"

TEMPLATE_SYNTAX = re.compile(r"\{%.*?%\}|\{\{.*?\}\}|\{#.*?#\}", re.DOTALL)
URL_ATTRIBUTES = frozenset({"href", "src", "action", "formaction"})

MARKUP_DEFECTS = {
    "inline event handler": re.compile(r"<[^>]*\son[a-z]+\s*=", re.IGNORECASE),
    "inline script": re.compile(r"<script(?![^>]*\ssrc=)[^>]*>", re.IGNORECASE),
    "inline style attribute": re.compile(r"<[^>]*\sstyle\s*=", re.IGNORECASE),
    "style element": re.compile(r"<style\b", re.IGNORECASE),
    "javascript: URL": re.compile(r"javascript:", re.IGNORECASE),
}

SCRIPT_DEFECTS = {
    "event handler property": re.compile(r"\.on[a-z]+\s*=(?!=)"),
    "event handler or style attribute": re.compile(
        r"setAttribute\(\s*['\"](on[a-z]+|style)['\"]", re.IGNORECASE
    ),
    "inline handler in markup": re.compile(r"<[^>]*\son[a-z]+\s*=", re.IGNORECASE),
    "inline style in markup": re.compile(r"<[^>]*\sstyle\s*=", re.IGNORECASE),
    "string evaluation": re.compile(
        r"\beval\(|new Function\(|set(Timeout|Interval)\(\s*['\"`]"
    ),
    "javascript: URL": re.compile(r"javascript:", re.IGNORECASE),
}


def defects(text: str, patterns: dict[str, re.Pattern[str]]) -> list[str]:
    return [name for name, pattern in patterns.items() if pattern.search(text)]


class InlineCodeFinder(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.findings: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        names = dict(attrs)
        self.findings += [f"<{tag} {name}>" for name in names if name.startswith("on")]
        self.findings += [
            f"<{tag} {name}=javascript:>"
            for name, value in names.items()
            if name in URL_ATTRIBUTES
            and (value or "").strip().lower().startswith("javascript:")
        ]
        if "style" in names:
            self.findings.append(f"<{tag} style>")
        if tag == "style":
            self.findings.append("<style>")
        if (
            tag == "script"
            and "src" not in names
            and names.get("type") != "application/json"
        ):
            self.findings.append("<script> without src")


def inline_code(html: str) -> list[str]:
    finder = InlineCodeFinder()
    finder.feed(html)
    finder.close()
    return finder.findings


def template_inline_code(source: str) -> list[str]:
    return inline_code(TEMPLATE_SYNTAX.sub(" ", source))


class PolicySettingsTests(TestCase):
    def test_site_policy_is_strict(self) -> None:
        policy = settings.OPENBENCH_CSP
        self.assertEqual(policy["script-src"], ("'self'",))
        self.assertEqual(policy["object-src"], ("'none'",))
        self.assertEqual(policy["frame-ancestors"], ("'none'",))
        self.assertNotIn("'unsafe-inline'", serialize_policy(policy))
        self.assertNotIn("'unsafe-eval'", serialize_policy(policy))

    def test_admin_policy_is_strict(self) -> None:
        self.assertNotIn("unsafe", serialize_policy(settings.OPENBENCH_CSP_ADMIN))

    def test_serialization(self) -> None:
        policy = {"default-src": ("'self'",), "img-src": ("'self'", "data:")}
        self.assertEqual(
            serialize_policy(policy), "default-src 'self'; img-src 'self' data:"
        )


class MiddlewareTests(TestCase):
    def setUp(self) -> None:
        self.site_policy = serialize_policy(settings.OPENBENCH_CSP)
        self.admin_policy = serialize_policy(settings.OPENBENCH_CSP_ADMIN)

    def test_html_page_carries_the_site_policy(self) -> None:
        response = self.client.get("/login/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response[HEADER], self.site_policy)

    def test_json_response_carries_the_site_policy(self) -> None:
        response = self.client.get("/api/config/")
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertEqual(response[HEADER], self.site_policy)

    def test_redirect_carries_the_site_policy(self) -> None:
        response = self.client.get("/index/")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response[HEADER], self.site_policy)

    def test_admin_carries_the_admin_policy(self) -> None:
        response = self.client.get("/admin/login/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response[HEADER], self.admin_policy)

    @override_settings(OPENBENCH_CSP={"default-src": ("'none'",)})
    def test_policy_is_read_from_settings(self) -> None:
        self.assertEqual(self.client.get("/login/")[HEADER], "default-src 'none'")

    def test_a_view_policy_is_kept(self) -> None:
        middleware = ContentSecurityPolicyMiddleware(
            lambda request: HttpResponse(headers={HEADER: "sandbox"})
        )
        response = middleware(RequestFactory().get("/index/"))
        self.assertEqual(response[HEADER], "sandbox")


class TemplateSourceTests(TestCase):
    def test_templates_carry_no_inline_code(self) -> None:
        self.assertTrue(TEMPLATES)
        found = {
            str(path.relative_to(ROOT)): problems
            for path in TEMPLATES
            if (problems := template_inline_code(path.read_text()))
        }
        self.assertEqual(found, {})

    def test_template_tags_emit_no_inline_code(self) -> None:
        self.assertEqual(defects(MYTAGS.read_text(), MARKUP_DEFECTS), [])

    def test_scripts_attach_no_inline_code(self) -> None:
        self.assertTrue(SCRIPTS)
        found = {
            path.name: problems
            for path in SCRIPTS
            if (problems := defects(path.read_text(), SCRIPT_DEFECTS))
        }
        self.assertEqual(found, {})

    def test_the_template_scanner_reads_through_template_tags(self) -> None:
        self.assertEqual(
            template_inline_code(
                '<input {% if a > b %}checked{% endif %} ONCLICK="go()" value="{{ x }}">'
                '<p {% if a > b %}Style="x"{% endif %}></p>'
                '<a href="{% if a %}javascript:go(){% endif %}JavaScript:go()"></a>'
                "<script>go()</script><style></style>"
            ),
            [
                "<input onclick>",
                "<p style>",
                "<a href=javascript:>",
                "<script> without src",
                "<style>",
            ],
        )
        self.assertEqual(
            template_inline_code(
                "<script src=\"{% static 'a.js' %}?{{ v }}\" defer></script>"
                '<p class="{% if a > b %}x{% endif %}">{{ a|json_script:"b" }}</p>'
            ),
            [],
        )

    def test_the_markup_scanner_catches_each_defect(self) -> None:
        self.assertEqual(
            defects(
                '<a onclick="go()" STYLE="x"></a><script>go()</script><style></style>'
                '<a href="javascript:go()">',
                MARKUP_DEFECTS,
            ),
            list(MARKUP_DEFECTS),
        )

    def test_the_script_scanner_catches_each_defect(self) -> None:
        self.assertEqual(
            defects(
                "el.onclick = go; el.setAttribute('onclick', 'go()');"
                " el.innerHTML = '<b onmouseover=go() STYLE=\"x\">'; eval('go()');"
                " link.href = 'javascript:go()';",
                SCRIPT_DEFECTS,
            ),
            list(SCRIPT_DEFECTS),
        )

    def test_the_page_scanner_catches_each_defect(self) -> None:
        self.assertEqual(
            inline_code(
                '<script type="application/json">{}</script><script src="/a.js"></script>'
            ),
            [],
        )
        self.assertEqual(
            len(
                inline_code(
                    '<b onclick=x style=y></b><button formaction=" javascript:x">'
                    "<script>x</script><style>"
                )
            ),
            5,
        )


class RenderedPageTests(TestCase):
    def setUp(self) -> None:
        create_engine_config()
        self.book = ensure_book()
        user = create_user("admin", approver=True)
        user.is_superuser = user.is_staff = True
        user.save()
        self.client.force_login(user)
        self.test = create_test(user, dev_options="Threads=1 Hash=16 <b onclick=x>")
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
        self.machine = Machine.objects.create(
            user=user, info={**system_info(), "supported": ["Avalanche"]}
        )
        Network.objects.create(
            sha256="ABCDEF01", name="r1", engine="Avalanche", author="admin"
        )

    def assert_renders_no_inline_code(self, page: str) -> None:
        response = self.client.get(page)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(inline_code(response.content.decode()), [])

    def test_pages_render_no_inline_code(self) -> None:
        pages = [
            "/index/",
            "/greens/",
            "/search/?go=1&keywords=x",
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
            "/progress/Avalanche/?window=all",
        ]
        for page in pages:
            with self.subTest(page=page):
                self.assert_renders_no_inline_code(page)

    def test_login_renders_no_inline_code(self) -> None:
        self.client.logout()
        self.assert_renders_no_inline_code("/login/")

    def test_register_renders_no_inline_code(self) -> None:
        self.client.logout()
        with mock.patch.dict(OPENBENCH_CONFIG, {"require_manual_registration": False}):
            self.assert_renders_no_inline_code("/register/")


class CsrfFailureTests(TestCase):
    def test_failure_page_fits_the_policy(self) -> None:
        client = Client(enforce_csrf_checks=True)
        response = client.post("/login/", {"username": "x", "password": "y"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response[HEADER], serialize_policy(settings.OPENBENCH_CSP))
        content = response.content.decode()
        self.assertIn("CSRF verification failed", content)
        self.assertIn(REASON_NO_CSRF_COOKIE, content)
        self.assertIn('<div id="sidebar">', content)
        self.assertEqual(inline_code(content), [])
