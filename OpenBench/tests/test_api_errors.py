import tempfile
import time
from pathlib import Path
from unittest import mock

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils.http import http_date, parse_http_date

from OpenBench.config import OPENBENCH_CONFIG
from OpenBench.models import Network
from OpenBench.tests.fixtures import (
    create_engine_config,
    create_test,
    create_user,
    credentials,
)

AUTH_SERVER = {"error": "API requires authentication for this server"}
AUTH_ENDPOINT = {"error": "API requires authentication for this endpoint"}
ENGINE_NOT_FOUND = {"error": "Engine not found. Check /api/config/ for a full list"}
NO_WORKLOAD = {"error": "Requested Workload Id does not exist"}
NETWORK_MAX_AGE = 7 * 24 * 60 * 60

VIEW_PATHS = (
    "/api/config/",
    "/api/config/Avalanche/",
    "/api/buildinfo/",
    "/api/networks/Avalanche/",
    "/api/pgns/%d/",
    "/api/spsa/%d/inputs/",
    "/api/workload/%d/info/",
    "/api/workload/%d/results/",
    "/api/insights/server/",
)


class ApiTestCase(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.media = tempfile.TemporaryDirectory()
        self.addCleanup(self.media.cleanup)
        self.enterContext(override_settings(MEDIA_ROOT=self.media.name))
        self.enterContext(mock.patch("OpenBench.utils.MEDIA_ROOT", self.media.name))
        create_engine_config()
        self.reader = create_user("reader")
        self.approver = create_user("approver", approver=True)
        self.workload = create_test(self.reader)

    def add_network(self, sha256, name, **fields):
        Path(self.media.name, sha256).write_bytes(sha256.encode())
        return Network.objects.create(
            sha256=sha256, name=name, engine="Avalanche", author="approver", **fields
        )

    def post(self, path, user=None, **data):
        return self.client.post(path, {**(credentials(user) if user else {}), **data})

    def assertAnswer(self, response, status, body):
        self.assertEqual(response.status_code, status, response.content)
        self.assertEqual(response.json(), body)


class AuthenticationStatusTests(ApiTestCase):
    def paths(self):
        return [path.replace("%d", str(self.workload.id)) for path in VIEW_PATHS]

    def test_view_endpoints_answer_401_without_credentials(self):
        for path in self.paths():
            with self.subTest(path=path):
                self.assertAnswer(self.client.get(path), 401, AUTH_SERVER)
                self.assertAnswer(
                    self.post(path, username="reader", password="wrong"),
                    401,
                    AUTH_SERVER,
                )

    def test_disabled_account_answers_401(self):
        disabled = create_user("disabled", enabled=False)
        for path in self.paths():
            with self.subTest(path=path):
                self.assertAnswer(self.post(path, disabled), 401, AUTH_SERVER)

    def test_download_answers_401_without_credentials(self):
        self.add_network("ABCDEF01", "r1")
        self.assertAnswer(
            self.client.get("/api/networks/Avalanche/r1/"), 401, AUTH_ENDPOINT
        )

    def test_public_server_still_serves_anonymous_readers(self):
        with mock.patch.dict(OPENBENCH_CONFIG, {"require_login_to_view": False}):
            self.assertEqual(self.client.get("/api/config/").status_code, 200)


class NetworkDeleteStatusTests(ApiTestCase):
    def delete(self, user=None, identifier="r1", **data):
        return self.post(f"/api/networks/Avalanche/{identifier}/delete/", user, **data)

    def test_anonymous_and_bad_credentials_answer_401(self):
        self.add_network("ABCDEF01", "r1")
        self.assertAnswer(self.delete(), 401, AUTH_ENDPOINT)
        self.assertAnswer(
            self.delete(username="approver", password="wrong"), 401, AUTH_ENDPOINT
        )
        self.assertTrue(Network.objects.exists())

    def test_non_approver_answers_403(self):
        self.add_network("ABCDEF01", "r1")
        self.assertAnswer(
            self.delete(self.reader),
            403,
            {"error": "Only Approvers may delete Networks"},
        )
        self.assertTrue(Network.objects.exists())

    def test_unknown_network_answers_404(self):
        self.assertAnswer(
            self.delete(self.approver, "nope"),
            404,
            {"error": "Network nope for Engine Avalanche not found"},
        )

    def test_default_network_answers_409(self):
        self.add_network("ABCDEF01", "r1", default=True, was_default=True)
        self.assertAnswer(
            self.delete(self.approver),
            409,
            {"error": "You may not delete Default, or previous Default networks"},
        )


class NotFoundStatusTests(ApiTestCase):
    def test_unknown_engine_answers_404(self):
        for path in [
            "/api/config/Nope/",
            "/api/networks/Nope/",
            "/api/networks/Nope/r1/",
        ]:
            with self.subTest(path=path):
                self.assertAnswer(self.post(path, self.reader), 404, ENGINE_NOT_FOUND)

    def test_unknown_network_of_a_known_engine_answers_404(self):
        self.assertAnswer(
            self.post("/api/networks/Avalanche/nope/", self.reader),
            404,
            {"error": "Network nope for Engine Avalanche not found"},
        )

    def test_unknown_workload_answers_404(self):
        for path in [
            "/api/workload/999/info/",
            "/api/spsa/999/inputs/",
            "/api/pgns/999/",
        ]:
            with self.subTest(path=path):
                self.assertAnswer(self.post(path, self.reader), 404, NO_WORKLOAD)

    def test_unknown_queries_answer_404(self):
        response = self.post(f"/api/workload/{self.workload.id}/nothing/", self.reader)
        self.assertEqual(response.status_code, 404)
        self.assertIn("Valid /query/ endpoints", response.json()["error"])

    def test_missing_pgn_archive_answers_404(self):
        self.workload.finished = True
        self.workload.save()
        self.assertAnswer(
            self.post(f"/api/pgns/{self.workload.id}/", self.reader),
            404,
            {"error": f"Unable to find PGN for Workload #{self.workload.id}"},
        )

    def test_archive_of_an_active_workload_answers_409(self):
        Path(self.media.name, "PGNs").mkdir()
        Path(self.media.name, "PGNs", f"{self.workload.id}.pgn.tar").write_bytes(b"tar")
        self.assertAnswer(
            self.post(f"/api/pgns/{self.workload.id}/", self.reader),
            409,
            {"error": "PGNs cannot be downloaded while the Workload is active"},
        )


class NetworkIdentifierTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.by_sha = self.add_network("ABCDEF01", "first")
        self.by_name = self.add_network("12345678", "ABCDEF01")

    def test_download_prefers_the_sha(self):
        response = self.post("/api/networks/Avalanche/ABCDEF01/", self.reader)
        self.assertEqual(
            response["Content-Disposition"], "attachment; filename=ABCDEF01"
        )

    def test_delete_prefers_the_sha_like_download(self):
        response = self.post("/api/networks/Avalanche/ABCDEF01/delete/", self.approver)
        self.assertAnswer(response, 200, {"success": "Deleted first for Avalanche"})
        self.assertEqual(
            list(Network.objects.values_list("name", flat=True)), ["ABCDEF01"]
        )

    def test_names_still_resolve(self):
        response = self.post("/api/networks/Avalanche/first/", self.reader)
        self.assertEqual(
            response["Content-Disposition"], "attachment; filename=ABCDEF01"
        )


class DownloadCachingHeaderTests(ApiTestCase):
    def test_network_download_expires_in_a_week_privately(self):
        self.add_network("ABCDEF01", "r1")
        response = self.post("/api/networks/Avalanche/r1/", self.reader)
        expires = parse_http_date(response["Expires"])
        self.assertEqual(response["Expires"], http_date(expires))
        self.assertAlmostEqual(expires - time.time(), NETWORK_MAX_AGE, delta=60)
        self.assertEqual(
            set(response["Cache-Control"].split(", ")),
            {"private", f"max-age={NETWORK_MAX_AGE}"},
        )

    def test_pgn_archive_is_never_cached(self):
        self.workload.finished = True
        self.workload.save()
        Path(self.media.name, "PGNs").mkdir()
        Path(self.media.name, "PGNs", f"{self.workload.id}.pgn.tar").write_bytes(b"tar")
        response = self.post(f"/api/pgns/{self.workload.id}/", self.reader)
        self.assertEqual(response.status_code, 200)
        self.assertIn("no-store", response["Cache-Control"])
        self.assertEqual(
            response["Expires"], http_date(parse_http_date(response["Expires"]))
        )


class ResultsFormattingTests(ApiTestCase):
    def test_results_are_formatted_like_the_other_queries(self):
        for query in ("results", "info"):
            with self.subTest(query=query):
                response = self.post(
                    f"/api/workload/{self.workload.id}/{query}/", self.reader
                )
                self.assertEqual(response["Content-Type"], "application/json")
                self.assertTrue(
                    response.content.startswith(b'{\n    "'), response.content[:40]
                )
