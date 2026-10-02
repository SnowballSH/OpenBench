import os
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

ROOT = Path(settings.BASE_DIR)

SETUP = 'import django; django.setup(); '
TEMPLATE_ENGINES_THEN_WSGI = SETUP + 'from django.template import engines; engines.all(); import OpenSite.wsgi'

# The test runner imports the URL configuration before anything else, which hides an import cycle that
# only bites when another module is the first one in: gunicorn starts at the WSGI module, and the
# template check loads the tag library before any view
FIRST_IMPORTS = (
    'OpenBench.templatetags.mytags',
    'OpenBench.views',
    'OpenBench.utils',
    'OpenBench.page_queries',
    'OpenBench.listing_rows',
    'OpenBench.navigation.catalogue',
    'OpenBench.insights.api',
    'OpenBench.diagnosis.api',
    'OpenBench.diagnosis.domain',
    'OpenBench.diagnosis.eligibility',
    'OpenBench.diagnosis.engine_support',
    'OpenBench.diagnosis.fleet',
    'OpenBench.diagnosis.listing',
    'OpenBench.diagnosis.reasoning',
    'OpenBench.diagnosis.report',
    'OpenBench.diagnosis.scheduler',
    'OpenBench.diagnosis.sources',
    'OpenBench.diagnosis.standing',
    'OpenBench.diagnosis.views',
    'OpenBench.progress.conditions',
    'OpenBench.progress.anchor',
    'OpenBench.progress.report',
    'OpenBench.progress.views',
    'OpenBench.releases.domain',
    'OpenBench.releases.github',
    'OpenBench.releases.service',
    'OpenBench.releases.store',
    'OpenBench.workloads.release_measurement',
    'OpenBench.management.commands.refresh_releases',
    'OpenBench.management.commands.set_release',
    'OpenBench.pgn_watcher',
    'OpenBench.workloads.clone',
    'OpenBench.workloads.confirmation',
    'OpenBench.workloads.page',
    'OpenBench.workloads.presets',
    'OpenBench.live.display',
    'OpenBench.live.domain',
    'OpenBench.live.listing',
    'OpenBench.live.token',
    'OpenBench.live.views',
    'OpenBench.live.workload',
    'OpenBench.digest.domain',
    'OpenBench.digest.errors',
    'OpenBench.digest.fleet',
    'OpenBench.digest.headline',
    'OpenBench.digest.present',
    'OpenBench.digest.report',
    'OpenBench.digest.serialize',
    'OpenBench.digest.sources',
    'OpenBench.digest.trunk',
    'OpenBench.digest.views',
    'OpenBench.digest.window',
    'OpenBench.digest.workloads',
)


def cold_python(data_dir: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    environment = {
        **os.environ,
        'DJANGO_SETTINGS_MODULE': 'OpenSite.settings',
        'OPENBENCH_SECRET_KEY': 'test',
        'OPENBENCH_DATA_DIR': data_dir,
    }
    return subprocess.run(
        [sys.executable, *arguments],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )


class ColdStartTests(SimpleTestCase):
    def assert_all_start(self, commands: dict[str, tuple[str, ...]]) -> None:
        with tempfile.TemporaryDirectory() as data_dir, ThreadPoolExecutor(len(commands)) as pool:
            results = dict(
                zip(
                    commands,
                    pool.map(lambda arguments: cold_python(data_dir, *arguments), commands.values()),
                    strict=True,
                )
            )
        for name, result in results.items():
            with self.subTest(start=name):
                self.assertEqual(result.returncode, 0, result.stderr[-2000:])

    def test_production_entry_points_start(self) -> None:
        self.assert_all_start(
            {
                'manage.py check': ('manage.py', 'check'),
                'template engines, then wsgi': ('-c', TEMPLATE_ENGINES_THEN_WSGI),
            }
        )

    def test_any_module_can_be_the_first_import(self) -> None:
        self.assert_all_start({module: ('-c', f'{SETUP}import {module}') for module in FIRST_IMPORTS})
