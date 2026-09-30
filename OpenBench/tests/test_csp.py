import re
from html.parser import HTMLParser
from pathlib import Path

from django.conf import settings
from django.http import HttpResponse
from django.test import RequestFactory, TestCase, override_settings

from OpenBench.models import Network
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
)

ROOT = Path(settings.BASE_DIR)
TEMPLATES = sorted((ROOT / "Templates").rglob("*.html"))
SCRIPTS = sorted((ROOT / "OpenBench" / "static").glob("*.js"))
MYTAGS = ROOT / "OpenBench" / "templatetags" / "mytags.py"

TEMPLATE_DEFECTS = {
    "inline event handler": re.compile(r"<[^>]*\son[a-z]+\s*=", re.IGNORECASE),
    "inline script": re.compile(r"<script(?![^>]*\ssrc=)[^>]*>", re.IGNORECASE),
    "inline style attribute": re.compile(r"<[^>]*\sstyle\s*=", re.IGNORECASE),
    "style element": re.compile(r"<style\b", re.IGNORECASE),
    "javascript: URL": re.compile(r"javascript:", re.IGNORECASE),
}

SCRIPT_DEFECTS = {
    "event handler property": re.compile(r"\.on[a-z]+\s*=(?!=)"),
    "event handler or style attribute": re.compile(
        r"setAttribute\(\s*['\"](on[a-z]+|style)['\"]"
    ),
    "inline handler in markup": re.compile(r"<[^>]*\son[a-z]+\s*=", re.IGNORECASE),
    "inline style in markup": re.compile(r"<[^>]*\sstyle\s*="),
    "string evaluation": re.compile(
        r"\beval\(|new Function\(|set(Timeout|Interval)\(\s*['\"`]"
    ),
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
    return finder.findings


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
            if (problems := defects(path.read_text(), TEMPLATE_DEFECTS))
        }
        self.assertEqual(found, {})

    def test_template_tags_emit_no_inline_code(self) -> None:
        self.assertEqual(defects(MYTAGS.read_text(), TEMPLATE_DEFECTS), [])

    def test_scripts_attach_no_inline_code(self) -> None:
        self.assertTrue(SCRIPTS)
        found = {
            path.name: problems
            for path in SCRIPTS
            if (problems := defects(path.read_text(), SCRIPT_DEFECTS))
        }
        self.assertEqual(found, {})

    def test_the_scanners_catch_each_defect(self) -> None:
        self.assertEqual(
            defects(
                '<a onclick="go()" style="x"></a><script>go()</script><style></style>'
                '<a href="javascript:go()">',
                TEMPLATE_DEFECTS,
            ),
            list(TEMPLATE_DEFECTS),
        )
        self.assertEqual(
            defects(
                "el.onclick = go; el.setAttribute('onclick', 'go()');"
                " el.innerHTML = '<b onmouseover=go() style=\"x\">'; eval('go()');",
                SCRIPT_DEFECTS,
            ),
            list(SCRIPT_DEFECTS),
        )
        self.assertEqual(
            inline_code(
                '<script type="application/json">{}</script><script src="/a.js"></script>'
            ),
            [],
        )
        self.assertEqual(
            len(inline_code("<b onclick=x style=y></b><script>x</script><style>")), 4
        )


class RenderedPageTests(TestCase):
    def setUp(self) -> None:
        create_engine_config()
        ensure_book()
        user = create_user("admin", approver=True)
        user.is_superuser = user.is_staff = True
        user.save()
        self.client.force_login(user)
        self.test = create_test(user, dev_options="Threads=1 Hash=16 <b onclick=x>")
        Network.objects.create(
            sha256="ABCDEF01", name="r1", engine="Avalanche", author="admin"
        )

    def test_pages_render_no_inline_code(self) -> None:
        pages = [
            "/index/",
            "/greens/",
            "/search/?go=1&keywords=x",
            f"/test/{self.test.id}/",
            "/test/new/",
            f"/test/new/?clone={self.test.id}",
            "/tune/new/",
            "/datagen/new/",
            "/machines/",
            "/users/",
            "/events/",
            "/errors/",
            "/networks/",
            "/networks/Avalanche/",
            "/newNetwork/",
            "/profile/",
            "/manage/books/",
            "/manage/engines/",
            "/manage/engines/Avalanche/",
            "/manage/storage/",
        ]
        for page in pages:
            with self.subTest(page=page):
                response = self.client.get(page)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(inline_code(response.content.decode()), [])

    def test_login_renders_no_inline_code(self) -> None:
        self.client.logout()
        response = self.client.get("/login/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(inline_code(response.content.decode()), [])
