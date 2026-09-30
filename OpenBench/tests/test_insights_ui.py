from django.test import TestCase

from OpenBench.templatetags.mytags import RowProgress, workload_progress
from OpenBench.tests.fixtures import (
    PASSWORD,
    create_engine_config,
    create_test,
    create_user,
    ensure_book,
)


class WorkloadProgressTests(TestCase):
    def setUp(self):
        create_engine_config()
        ensure_book()
        self.author = create_user("author")

    def test_sprt_is_the_llr_position_between_the_bounds(self):
        progress = workload_progress(
            create_test(self.author, test_mode="SPRT", currentllr=1.47)
        )
        self.assertEqual(progress.kind, "llr")
        self.assertAlmostEqual(progress.fraction, 0.75)
        self.assertEqual(progress.label, "LLR 1.47 between bounds -2.94 and 2.94")

    def test_sprt_position_is_clamped_to_the_bounds(self):
        self.assertEqual(
            workload_progress(create_test(self.author, currentllr=3.1)).fraction, 1.0
        )
        self.assertEqual(
            workload_progress(create_test(self.author, currentllr=-3.1)).fraction, 0.0
        )

    def test_sprt_without_bounds_has_no_progress(self):
        self.assertIsNone(
            workload_progress(create_test(self.author, lowerllr=0.0, upperllr=0.0))
        )

    def test_games_and_datagen_are_games_over_the_target(self):
        for mode in ("GAMES", "DATAGEN"):
            progress = workload_progress(
                create_test(self.author, test_mode=mode, games=800, max_games=4000)
            )
            self.assertEqual(
                progress, RowProgress("games", 0.2, "800 of 4,000 games (20%)")
            )

    def test_games_fraction_is_capped(self):
        progress = workload_progress(
            create_test(self.author, test_mode="GAMES", games=4100, max_games=4000)
        )
        self.assertEqual(progress.fraction, 1.0)

    def test_games_without_a_target_and_spsa_have_no_progress(self):
        self.assertIsNone(
            workload_progress(create_test(self.author, test_mode="GAMES", max_games=0))
        )
        self.assertIsNone(workload_progress(create_test(self.author, test_mode="SPSA")))

    def test_progress_runs_no_queries(self):
        test = create_test(self.author, test_mode="GAMES", games=10, max_games=20)
        with self.assertNumQueries(0):
            workload_progress(test)


class InsightsPageTests(TestCase):
    def setUp(self):
        create_engine_config()
        ensure_book()
        self.author = create_user("author")
        self.client.post("/login/", {"username": "author", "password": PASSWORD})

    def test_index_has_the_server_strip_and_active_row_progress(self):
        create_test(self.author, test_mode="SPRT", currentllr=0.5)
        content = self.client.get("/index/").content.decode()
        self.assertIn("data-server-insights", content)
        self.assertIn("insights.js", content)
        self.assertIn("row-progress-llr", content)

    def test_finished_rows_have_no_progress(self):
        create_test(self.author, test_mode="SPRT", finished=True, passed=True)
        self.assertNotIn("row-progress", self.client.get("/index/").content.decode())

    def test_user_page_has_no_server_strip(self):
        self.assertNotIn(
            "data-server-insights", self.client.get("/user/author/").content.decode()
        )

    def test_workload_page_has_the_insights_section(self):
        test = create_test(self.author)
        content = self.client.get("/test/%d/" % test.id).content.decode()
        self.assertIn("data-workload-insights", content)
        self.assertIn("vendor/chartjs-4.5.1/chart.umd.min.js", content)
        self.assertIn("summary-container", content)
