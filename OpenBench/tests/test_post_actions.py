import importlib.util
import os
import re
import tempfile

from unittest import mock

from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from OpenSite.settings import BASE_DIR

from OpenBench.models import Book, EngineConfig, LogEvent, Network, Profile, Test
from OpenBench.tests.fixtures import (
    create_engine_config,
    create_test,
    create_user,
    credentials,
    ensure_book,
)

CSRF_INPUT = re.compile(
    r'<input [^>]*name="csrfmiddlewaretoken" value="([^"]+)"'
)


class CsrfClientMixin:
    def csrf_client(self, user):
        client = self.client_class(enforce_csrf_checks=True)
        client.force_login(user)
        return client

    def form_token(self, client, page):
        content = client.get(page).content.decode()
        self.assertRegex(content, CSRF_INPUT)
        return CSRF_INPUT.search(content).group(1)


class WorkloadActionTests(CsrfClientMixin, TestCase):
    STATES = {
        "APPROVE": ({"approved": False}, "approved", True),
        "RESTART": ({"finished": True}, "finished", False),
        "STOP": ({"finished": False}, "finished", True),
        "DELETE": ({"deleted": False}, "deleted", True),
        "RESTORE": ({"deleted": True}, "deleted", False),
    }

    def setUp(self):
        create_engine_config()
        ensure_book()
        self.author = create_user("author")
        self.approver = create_user("approver", approver=True)

    def workload(self, action):
        fields, _, _ = self.STATES[action]
        return create_test(self.author, **{"approved": True, **fields})

    def field(self, workload, action):
        _, name, _ = self.STATES[action]
        return getattr(Test.objects.get(id=workload.id), name)

    def test_post_with_a_csrf_token_applies_each_action(self):
        for action, (_, _, expected) in self.STATES.items():
            workload = self.workload(action)
            client = self.csrf_client(self.approver)
            token = self.form_token(client, "/test/%d/" % (workload.id))
            response = client.post(
                "/test/%d/%s/" % (workload.id, action), {"csrfmiddlewaretoken": token}
            )
            self.assertEqual(response.status_code, 302, action)
            self.assertEqual(self.field(workload, action), expected, action)

    def test_get_changes_nothing_and_returns_to_the_workload(self):
        self.client.force_login(self.approver)
        for action, (_, _, expected) in self.STATES.items():
            workload = self.workload(action)
            response = self.client.get("/test/%d/%s/" % (workload.id, action))
            self.assertRedirects(
                response, "/test/%d/" % (workload.id), fetch_redirect_response=False
            )
            self.assertIn("must be submitted", self.client.session["error_message"])
            self.assertNotEqual(self.field(workload, action), expected, action)
        self.assertFalse(LogEvent.objects.exists())

    def test_get_of_modify_changes_nothing(self):
        workload = create_test(self.author)
        self.client.force_login(self.author)
        self.client.get("/test/%d/MODIFY/?priority=9" % (workload.id))
        self.assertEqual(Test.objects.get(id=workload.id).priority, 0)
        self.assertFalse(LogEvent.objects.exists())

    def test_post_without_a_csrf_token_is_refused(self):
        client = self.csrf_client(self.approver)
        for action, (_, _, expected) in self.STATES.items():
            workload = self.workload(action)
            self.assertEqual(
                client.post("/test/%d/%s/" % (workload.id, action)).status_code,
                403,
                action,
            )
            self.assertNotEqual(self.field(workload, action), expected, action)
        self.assertFalse(LogEvent.objects.exists())

    def test_viewing_a_workload_is_still_a_get(self):
        workload = create_test(self.author)
        self.client.force_login(self.author)
        self.assertEqual(self.client.get("/test/%d/" % (workload.id)).status_code, 200)

    def test_buttons_submit_the_csrf_protected_form(self):
        workload = create_test(self.author, approved=False)
        self.client.force_login(self.approver)
        content = self.client.get("/test/%d/" % (workload.id)).content.decode()
        self.assertRegex(
            content,
            r'<form id="workload-actions" method="post" hidden><input [^>]*name="csrfmiddlewaretoken"',
        )
        for action in ("APPROVE", "STOP", "DELETE"):
            self.assertIn(
                'form="workload-actions" formaction="/test/%d/%s/"'
                % (workload.id, action),
                content,
            )
        self.assertNotRegex(content, r'href="/test/%d/[A-Z]+' % (workload.id))
        self.assertIn('data-confirm="Delete this Workload?"', content)


class NetworkActionTests(CsrfClientMixin, TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        create_engine_config()
        self.approver = create_user("admin", approver=True)
        self.old = Network.objects.create(
            sha256="AAAAAAAA",
            name="old",
            engine="Avalanche",
            author="admin",
            default=True,
        )
        self.new = Network.objects.create(
            sha256="BBBBBBBB", name="new", engine="Avalanche", author="admin"
        )

    def refreshed(self):
        self.old.refresh_from_db()
        self.new.refresh_from_db()

    def test_post_with_a_csrf_token_sets_the_default(self):
        client = self.csrf_client(self.approver)
        token = self.form_token(client, "/networks/")
        client.post(
            "/networks/Avalanche/DEFAULT/BBBBBBBB/", {"csrfmiddlewaretoken": token}
        )
        self.refreshed()
        self.assertEqual(
            (self.old.default, self.old.was_default, self.new.default),
            (False, True, True),
        )

    def test_post_with_a_csrf_token_deletes(self):
        client = self.csrf_client(self.approver)
        token = self.form_token(client, "/networks/")
        client.post(
            "/networks/Avalanche/DELETE/BBBBBBBB/", {"csrfmiddlewaretoken": token}
        )
        self.assertFalse(Network.objects.filter(id=self.new.id).exists())

    def test_get_changes_nothing_and_returns_to_the_networks(self):
        self.client.force_login(self.approver)
        for action in ("DEFAULT", "DELETE"):
            response = self.client.get("/networks/Avalanche/%s/BBBBBBBB/" % (action))
            self.assertRedirects(
                response, "/networks/Avalanche/", fetch_redirect_response=False
            )
            self.assertIn("must be submitted", self.client.session["error_message"])
        self.refreshed()
        self.assertEqual((self.old.default, self.new.default), (True, False))

    def test_post_without_a_csrf_token_is_refused(self):
        client = self.csrf_client(self.approver)
        for action in ("DEFAULT", "DELETE"):
            self.assertEqual(
                client.post("/networks/Avalanche/%s/BBBBBBBB/" % (action)).status_code,
                403,
            )
        self.refreshed()
        self.assertEqual((self.old.default, self.new.default), (True, False))

    def test_buttons_submit_the_csrf_protected_form(self):
        self.client.force_login(self.approver)
        content = self.client.get("/networks/").content.decode()
        self.assertRegex(
            content,
            r'<form id="network-actions" method="post" hidden><input [^>]*name="csrfmiddlewaretoken"',
        )
        self.assertIn(
            'form="network-actions" formaction="/networks/Avalanche/DEFAULT/BBBBBBBB/"',
            content,
        )
        self.assertRegex(
            content,
            r'formaction="/networks/Avalanche/DELETE/BBBBBBBB/"\s+data-confirm="',
        )
        self.assertNotRegex(content, r'href="/networks/[^"]+/(DEFAULT|DELETE)/')

    def test_downloads_stay_a_get(self):
        with (
            tempfile.TemporaryDirectory() as media,
            mock.patch("OpenBench.utils.MEDIA_ROOT", media),
        ):
            with open(os.path.join(media, "BBBBBBBB"), "wb") as fout:
                fout.write(b"weights")

            self.client.force_login(self.approver)
            response = self.client.get("/networks/Avalanche/DOWNLOAD/BBBBBBBB/")
            self.assertEqual(b"".join(response.streaming_content), b"weights")

            self.client.logout()
            response = self.client.post(
                "/clientGetNetwork/Avalanche/new/",
                credentials(create_user("lab-worker")),
            )
            self.assertEqual(b"".join(response.streaming_content), b"weights")


class ManageActionTests(CsrfClientMixin, TestCase):
    def setUp(self):
        self.manager = create_user("manager")
        Profile.objects.filter(user=self.manager).update(superuser=True)
        self.book = Book.objects.create(
            name="unused.epd", source="https://example.invalid/unused.zip", sha="0" * 64
        )
        self.engine = create_engine_config("Unused")

    def test_get_deletes_nothing(self):
        self.client.force_login(self.manager)
        for path, listing in (
            ("/manage/books/unused.epd/delete/", "/manage/books/"),
            ("/manage/engines/Unused/delete/", "/manage/engines/"),
        ):
            self.assertRedirects(
                self.client.get(path), listing, fetch_redirect_response=False
            )
            self.assertIn("must be submitted", self.client.session["error_message"])
        self.assertTrue(Book.objects.filter(id=self.book.id).exists())
        self.assertTrue(EngineConfig.objects.filter(id=self.engine.id).exists())

    def test_post_without_a_csrf_token_is_refused(self):
        client = self.csrf_client(self.manager)
        self.assertEqual(
            client.post("/manage/books/unused.epd/delete/").status_code, 403
        )
        self.assertEqual(client.post("/manage/engines/Unused/delete/").status_code, 403)
        self.assertTrue(Book.objects.filter(id=self.book.id).exists())
        self.assertTrue(EngineConfig.objects.filter(id=self.engine.id).exists())

    def test_post_with_a_csrf_token_deletes(self):
        client = self.csrf_client(self.manager)
        token = self.form_token(client, "/manage/books/")
        client.post("/manage/books/unused.epd/delete/", {"csrfmiddlewaretoken": token})
        self.assertFalse(Book.objects.filter(id=self.book.id).exists())

        token = self.form_token(client, "/manage/engines/")
        client.post("/manage/engines/Unused/delete/", {"csrfmiddlewaretoken": token})
        self.assertFalse(EngineConfig.objects.filter(id=self.engine.id).exists())

    def test_delete_buttons_submit_the_csrf_protected_form(self):
        self.client.force_login(self.manager)
        content = self.client.get("/manage/books/").content.decode()
        self.assertIn('form="manage-actions"', content)
        self.assertNotIn('href="/manage/books/unused.epd/delete/"', content)


class ManageGetRefusalTests(TestCase):
    def setUp(self):
        self.manager = create_user("manager")
        Profile.objects.filter(user=self.manager).update(superuser=True)
        self.book = Book.objects.create(
            name="unused.epd", source="https://example.invalid/unused.zip", sha="0" * 64
        )
        self.engine = create_engine_config("Unused")
        self.client.force_login(self.manager)

    def assert_refused(self, path, listing):
        self.assertRedirects(
            self.client.get(path), listing, fetch_redirect_response=False
        )
        self.assertIn("must be submitted", self.client.session["error_message"])

    def test_get_of_book_create_and_edit_changes_nothing(self):
        query = (
            "?source=https://raw.githubusercontent.com/x/y.zip&sha="
            + "a" * 64
            + "&enabled=TRUE"
        )
        self.assert_refused("/manage/books/fresh.epd/create/" + query, "/manage/books/")
        self.assert_refused("/manage/books/unused.epd/edit/" + query, "/manage/books/")
        self.assertFalse(Book.objects.filter(name="fresh.epd").exists())
        self.assertEqual(Book.objects.get(id=self.book.id).sha, "0" * 64)

    def test_get_of_engine_create_and_edit_changes_nothing(self):
        query = "?source=https://github.com/x/y&nps=5&private=FALSE&enabled=FALSE"
        self.assert_refused("/manage/engines/Fresh/create/" + query, "/manage/engines/")
        self.assert_refused("/manage/engines/Unused/edit/" + query, "/manage/engines/")
        self.assertFalse(EngineConfig.objects.filter(name="Fresh").exists())
        self.assertEqual(EngineConfig.objects.get(id=self.engine.id).nps, 1000000)


class NetworkUploadAndEditTests(CsrfClientMixin, TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        media = tempfile.TemporaryDirectory()
        self.addCleanup(media.cleanup)
        self.enterContext(override_settings(MEDIA_ROOT=media.name))
        create_engine_config()
        self.approver = create_user("admin", approver=True)

    def netfile(self):
        return SimpleUploadedFile("net.nnue", b"weights")

    def test_website_upload_with_a_csrf_token(self):
        client = self.csrf_client(self.approver)
        token = self.form_token(client, "/newNetwork/")
        response = client.post(
            "/networks/Avalanche/upload/r1/",
            {"csrfmiddlewaretoken": token, "netfile": self.netfile()},
        )
        self.assertRedirects(
            response, "/networks/Avalanche/", fetch_redirect_response=False
        )
        self.assertEqual(Network.objects.get().name, "r1")

    def test_website_upload_without_a_csrf_token_is_refused(self):
        client = self.csrf_client(self.approver)
        response = client.post(
            "/networks/Avalanche/upload/r1/", {"netfile": self.netfile()}
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Network.objects.exists())

    def test_cross_site_upload_is_refused(self):
        self.client.force_login(self.approver)
        for site in ("cross-site", "same-site"):
            self.client.post(
                "/networks/Avalanche/upload/r1/",
                {"netfile": self.netfile()},
                headers={"sec-fetch-site": site},
            )
        self.assertFalse(Network.objects.exists())

    def test_scripts_upload_needs_no_csrf_token(self):
        client = self.client_class(enforce_csrf_checks=True)
        response = client.post(
            "/scripts/",
            {
                **credentials(self.approver),
                "action": "UPLOAD_NETWORK",
                "engine": "Avalanche",
                "name": "r1",
                "netfile": self.netfile(),
            },
        )
        self.assertRedirects(
            response, "/networks/Avalanche/", fetch_redirect_response=False
        )
        self.assertEqual(Network.objects.get().author, "admin")

    def test_cross_site_edit_is_refused(self):
        Network.objects.create(
            sha256="ABCDEF01", name="r1", engine="Avalanche", author="admin"
        )
        edit = {"name": "renamed", "default": "FALSE", "was_default": "FALSE"}
        self.client.force_login(self.approver)
        self.client.post(
            "/networks/Avalanche/EDIT/ABCDEF01/",
            edit,
            headers={"sec-fetch-site": "cross-site"},
        )
        self.assertEqual(Network.objects.get().name, "r1")
        self.client.post(
            "/networks/Avalanche/EDIT/ABCDEF01/",
            edit,
            headers={"sec-fetch-site": "same-origin"},
        )
        self.assertEqual(Network.objects.get().name, "renamed")

    def test_edit_form_is_still_served_to_cross_site_links(self):
        Network.objects.create(
            sha256="ABCDEF01", name="r1", engine="Avalanche", author="admin"
        )
        self.client.force_login(self.approver)
        response = self.client.get(
            "/networks/Avalanche/EDIT/ABCDEF01/",
            headers={"sec-fetch-site": "cross-site"},
        )
        self.assertEqual(response.status_code, 200)


class UploadScriptBannerTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        path = os.path.join(BASE_DIR, "Scripts", "upload_net.py")
        spec = importlib.util.spec_from_file_location("upload_net", path)
        self.script = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.script)

    def test_banners_are_read_from_a_rendered_page(self):
        response = self.client.post(
            "/scripts/",
            {"username": "nobody", "password": "wrong", "action": "UPLOAD_NETWORK"},
            follow=True,
        )
        banners = self.script.page_banners(response.content.decode())
        self.assertEqual(banners.get("error-message"), "Unable to authenticate user")


class ApiNetworkDeleteSessionTests(CsrfClientMixin, TestCase):
    def setUp(self):
        create_engine_config()
        self.approver = create_user("admin", approver=True)
        self.network = Network.objects.create(
            sha256="ABCDEF01", name="r1", engine="Avalanche", author="admin"
        )

    def test_session_without_a_csrf_token_is_refused(self):
        response = self.csrf_client(self.approver).post(
            "/api/networks/Avalanche/r1/delete/"
        )
        self.assertEqual(response.status_code, 403)
        self.assertTrue(Network.objects.filter(id=self.network.id).exists())

    def test_session_with_a_csrf_token_deletes(self):
        client = self.csrf_client(self.approver)
        token = self.form_token(client, "/networks/")
        self.assertIn(
            "success",
            client.post(
                "/api/networks/Avalanche/r1/delete/", {"csrfmiddlewaretoken": token}
            ).json(),
        )

    def test_credentials_without_a_session_need_no_token(self):
        client = self.client_class(enforce_csrf_checks=True)
        self.assertIn(
            "success",
            client.post(
                "/api/networks/Avalanche/r1/delete/", credentials(self.approver)
            ).json(),
        )
