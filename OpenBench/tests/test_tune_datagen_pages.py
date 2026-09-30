from django.test import TestCase

from OpenBench.models import Book, SPSAParameter, SPSARun, Test
from OpenBench.tests.fixtures import (
    create_engine_config,
    create_test,
    create_user,
    credentials,
    ensure_book,
)


class TuneAndDatagenPageTests(TestCase):
    def setUp(self) -> None:
        create_engine_config()
        ensure_book()
        self.author = create_user("author")
        self.client.post("/login/", credentials(self.author))

    def tune(self) -> Test:
        test = create_test(self.author, test_mode="SPSA", workload_size=8)
        run = SPSARun.objects.create(
            tune=test,
            reporting_type="BATCHED",
            distribution_type="SINGLE",
            alpha=0.602,
            gamma=0.101,
            iterations=100,
            pairs_per=8,
            a_ratio=0.1,
        )
        SPSAParameter.objects.create(
            spsa_run=run,
            name="RfpMargin",
            index=0,
            value=70.4,
            is_float=False,
            start=75,
            min_value=30,
            max_value=150,
            c_end=8,
            r_end=0.002,
            c_value=12.4,
            a_value=1.2,
        )
        return test

    def page(self, test: Test) -> str:
        return self.client.get(
            f"/{test.workload_type_str()}/{test.id}/"
        ).content.decode()

    def test_spsa_digest_numbers_are_right_aligned(self) -> None:
        content = self.page(self.tune())
        for header in ("Curr", "Start", "Min", "Max", "C", "C_end", "R", "R_end"):
            self.assertIn(f'<th class="numeric">{header}</th>', content)
        self.assertIn("<th>Name</th>", content)

    def test_a_book_without_a_download_is_plain_text(self) -> None:
        content = self.page(
            create_test(self.author, test_mode="DATAGEN", book_name="NONE")
        )
        self.assertIn("<td>NONE</td>", content)
        self.assertNotIn("<a>NONE</a>", content)

    def test_a_known_book_stays_a_link(self) -> None:
        test = create_test(self.author, test_mode="DATAGEN")
        self.assertIn(
            f'<a href="{Book.objects.get(name=test.book_name).source}">{test.book_name}</a>',
            self.page(test),
        )
