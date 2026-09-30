import tempfile
from unittest import mock

from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from OpenBench.models import Network, Test
from OpenBench.tests.fixtures import (
    create_engine_config,
    create_user,
    credentials,
    ensure_book,
)
from OpenBench.tests.test_create_workload import (
    github_commit,
    rendered_error,
    test_fields,
    tune_fields,
)


def session_error(client) -> str | None:
    session = client.session
    error = session.pop("error_message", None)
    session.save()
    return error


class ScriptsCreateTestFormTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        create_engine_config()
        ensure_book()
        self.author = create_user("author")

    def create(self, fields):
        payload = {**credentials(self.author), "action": "CREATE_TEST", **fields}
        with mock.patch("requests.get", side_effect=github_commit) as get:
            response = self.client.post("/scripts/", payload)
        return response, get

    def test_bare_action_is_a_validation_error(self):
        response, get = self.create({})
        self.assertEqual(response.status_code, 200)
        self.assertIn("Dev Engine was not found", rendered_error(response))
        get.assert_not_called()
        self.assertFalse(Test.objects.exists())

    def test_missing_branch_is_named_without_reaching_github(self):
        fields = test_fields()
        del fields["dev_branch"]
        response, _ = self.create(fields)
        self.assertEqual(response.status_code, 200)
        self.assertIn("dev_branch", rendered_error(response))
        self.assertFalse(Test.objects.exists())

    def test_missing_dev_fields_leave_base_verification_intact(self):
        fields = test_fields()
        for name in ("dev_branch", "dev_repo", "dev_engine"):
            del fields[name]
        response, _ = self.create(fields)
        self.assertEqual(response.status_code, 200)
        self.assertIn("dev_branch", rendered_error(response))

    def test_complete_form_still_creates(self):
        response, _ = self.create(test_fields())
        self.assertRedirects(response, "/index/", fetch_redirect_response=False)
        self.assertTrue(Test.objects.exists())


class WebsiteCreateFormMissingFieldTests(TestCase):
    def setUp(self):
        create_engine_config()
        ensure_book()
        self.client.force_login(create_user("author"))

    def post(self, kind, fields):
        with mock.patch("requests.get", side_effect=github_commit):
            return self.client.post(f"/{kind}/new/", fields)

    def test_empty_forms_are_validation_errors(self):
        for kind in ("test", "tune", "datagen"):
            with self.subTest(kind=kind):
                response = self.post(kind, {})
                self.assertEqual(response.status_code, 200)
                self.assertIsNotNone(rendered_error(response))

    def test_tune_without_info_is_created(self):
        fields = tune_fields()
        del fields["info"]
        response = self.post("tune", fields)
        self.assertRedirects(response, "/index/", fetch_redirect_response=False)
        self.assertEqual(Test.objects.get().info, "")

    def test_datagen_without_info_is_created(self):
        fields = test_fields(
            datagen_max_games="1000",
            datagen_custom_genfens="",
            datagen_play_reverses="NO",
        )
        del fields["info"]
        response = self.post("datagen", fields)
        self.assertRedirects(response, "/index/", fetch_redirect_response=False)
        self.assertEqual(Test.objects.get().info, "")


class ScriptsUploadNetworkFormTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        media = tempfile.TemporaryDirectory()
        self.addCleanup(media.cleanup)
        self.enterContext(override_settings(MEDIA_ROOT=media.name))
        self.enterContext(mock.patch("OpenBench.utils.MEDIA_ROOT", media.name))
        create_engine_config()
        self.approver = create_user("approver", approver=True)

    def upload(self, user, **fields):
        payload = {
            **credentials(user),
            "action": "UPLOAD_NETWORK",
            "engine": "Avalanche",
            "name": "r1",
            "netfile": SimpleUploadedFile("net.nnue", b"weights"),
            **fields,
        }
        return self.client.post(
            "/scripts/", {k: v for k, v in payload.items() if v is not None}
        )

    def test_missing_fields_are_reported(self):
        for field in ("engine", "name", "netfile"):
            with self.subTest(field=field):
                response = self.upload(self.approver, **{field: None})
                self.assertRedirects(
                    response, "/networks/", fetch_redirect_response=False
                )
                self.assertIn(field, session_error(self.client))
                self.assertFalse(Network.objects.exists())

    def test_non_approver_is_told_why(self):
        response = self.upload(create_user("worker"))
        self.assertRedirects(response, "/index/", fetch_redirect_response=False)
        self.assertEqual(
            session_error(self.client), "Only Approvers may upload Networks"
        )
        self.assertFalse(Network.objects.exists())

    def test_approver_upload_still_works(self):
        response = self.upload(self.approver)
        self.assertRedirects(
            response, "/networks/Avalanche/", fetch_redirect_response=False
        )
        self.assertEqual(Network.objects.get().name, "r1")


class WebsiteUploadWithoutFileTests(TestCase):
    def test_missing_file_is_an_error_not_a_crash(self):
        create_engine_config()
        self.client.force_login(create_user("approver", approver=True))
        response = self.client.post("/networks/Avalanche/UPLOAD/r1/")
        self.assertRedirects(response, "/networks/", fetch_redirect_response=False)
        self.assertIn("netfile", session_error(self.client))
        self.assertFalse(Network.objects.exists())
