from django.test import TestCase

from OpenBench.tests.fixtures import create_engine_config, create_user, ensure_book


class HeadRequestTests(TestCase):
    def test_login_page_answers_like_a_get(self) -> None:
        self.assertEqual(self.client.head('/login/').status_code, 200)

    def test_pages_with_forms_are_read_not_submitted(self) -> None:
        create_engine_config()
        ensure_book()
        user = create_user('reader')
        user.email = 'reader@example.invalid'
        user.save()
        self.client.force_login(user)

        for path in ('/profile/', '/profileConfig/', '/test/new/', '/tune/new/', '/datagen/new/'):
            with self.subTest(path=path):
                self.assertEqual(self.client.head(path).status_code, 200)

        user.refresh_from_db()
        self.assertEqual(user.email, 'reader@example.invalid')
