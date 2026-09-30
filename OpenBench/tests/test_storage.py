import json
import os
import shutil
import stat
import tempfile
from pathlib import Path
from unittest import mock

from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings

from OpenBench.models import LogEvent, Network, Profile
from OpenBench.storage.classify import classify
from OpenBench.storage.domain import GIB, Category, DiskUsage, MediaFile
from OpenBench.storage.present import format_bytes, storage_page
from OpenBench.storage.report import CACHE_KEY, build_report, current_report
from OpenBench.storage.scan import (
    Walker,
    database_usage,
    directory_usage,
    disk_usage,
    network_file_sizes,
    scan_media,
)
from OpenBench.tests.fixtures import (
    create_engine_config,
    create_test,
    create_user,
    credentials,
    ensure_book,
)


class MediaTree:
    def __init__(self, testcase: SimpleTestCase) -> None:
        directory = tempfile.TemporaryDirectory()
        testcase.addCleanup(directory.cleanup)
        self.root = Path(directory.name) / 'Media'
        self.root.mkdir()

    def write(self, relative: str, size: int) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'x' * size)
        return path

    def sizes(self, walker: Walker | None = None) -> dict[str, int]:
        return {file.path: file.size for file in scan_media(self.root, walker).files}


class ScanTests(SimpleTestCase):
    def setUp(self):
        self.media = MediaTree(self)

    def test_top_level_files_and_the_archive_directory_are_listed(self):
        self.media.write('ABCDEF12', 300)
        self.media.write('1.2.0.pgn.bz2', 20)
        self.media.write('PGNs/7.pgn.tar', 4000)
        self.media.write('event9.log', 5)
        self.assertEqual(
            self.media.sizes(),
            {
                'ABCDEF12': 300,
                '1.2.0.pgn.bz2': 20,
                'PGNs/7.pgn.tar': 4000,
                'event9.log': 5,
            },
        )

    def test_unknown_directories_are_summed_as_one_item(self):
        self.media.write('cache/a', 10)
        self.media.write('cache/deeper/b', 32)
        files = scan_media(self.media.root).files
        self.assertEqual(files, (MediaFile(path='cache/', size=42, is_dir=True),))

    def test_symlinks_are_never_followed(self):
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        target = Path(outside.name) / 'huge'
        target.write_bytes(b'x' * 1000)
        (self.media.root / 'linked-file').symlink_to(target)
        (self.media.root / 'linked-dir').symlink_to(outside.name, target_is_directory=True)
        self.media.write('cache/inner', 1)
        (self.media.root / 'cache' / 'escape').symlink_to(outside.name, target_is_directory=True)

        result = scan_media(self.media.root)
        self.assertEqual({file.path: file.size for file in result.files}, {'cache/': 1})
        self.assertEqual(result.skipped_symlinks, 3)

    def test_unreadable_directories_are_counted(self):
        if os.geteuid() == 0:
            self.skipTest('root reads every directory')
        locked = self.media.root / 'locked'
        self.media.write('locked/secret', 50)
        locked.chmod(0)
        self.addCleanup(locked.chmod, stat.S_IRWXU)
        result = scan_media(self.media.root)
        self.assertEqual(result.unreadable_dirs, 1)
        self.assertEqual(result.files, (MediaFile('locked/', 0, is_dir=True),))

    def test_a_missing_media_root_is_empty(self):
        result = scan_media(self.media.root / 'absent')
        self.assertEqual((result.files, result.skipped_symlinks, result.truncated), ((), 0, False))

    def test_a_file_deleted_mid_scan_is_skipped(self):
        doomed = self.media.write('1.1.0.pgn.bz2', 10)
        self.media.write('event1.log', 3)
        listing = Walker.entries

        def list_then_delete(walker, directory):
            entries = listing(walker, directory)
            doomed.unlink(missing_ok=True)
            return entries

        with mock.patch.object(Walker, 'entries', list_then_delete):
            self.assertEqual(self.media.sizes(), {'event1.log': 3})

    def test_the_entry_budget_bounds_the_scan(self):
        for index in range(5):
            self.media.write(f'file{index}', 1)
        result = scan_media(self.media.root, Walker(entries=3))
        self.assertEqual(len(result.files), 3)
        self.assertTrue(result.truncated)

    def test_database_usage_adds_the_wal_and_shared_memory_files(self):
        database = self.media.root.parent / 'db.sqlite3'
        database.write_bytes(b'x' * 100)
        Path(str(database) + '-wal').write_bytes(b'x' * 20)
        usage = database_usage(database)
        self.assertEqual((usage.main, usage.wal, usage.shm, usage.total), (100, 20, 0, 120))

    def test_disk_usage_measures_media_or_else_the_data_directory(self):
        data_dir = self.media.root.parent
        with mock.patch('shutil.disk_usage', wraps=shutil.disk_usage) as usage:
            self.assertIsNotNone(disk_usage(self.media.root, data_dir))
            self.assertIsNotNone(disk_usage(self.media.root / 'absent', data_dir))
        self.assertEqual([call.args[0] for call in usage.call_args_list], [self.media.root, data_dir])
        self.assertIsNone(disk_usage(data_dir / 'absent', data_dir / 'absent'))

    def test_network_files_are_checked_directly_without_following_links(self):
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        (Path(outside.name) / 'target').write_bytes(b'x' * 99)
        self.media.write('AAAAAAAA', 10)
        (self.media.root / 'BBBBBBBB').symlink_to(Path(outside.name) / 'target')
        (self.media.root / 'CCCCCCCC').mkdir()
        self.media.write('PGNs/DDDDDDDD', 5)
        shas = ['AAAAAAAA', 'BBBBBBBB', 'CCCCCCCC', 'PGNs/DDDDDDDD', '..', 'EEEEEEEE']
        self.assertEqual(network_file_sizes(self.media.root, shas), {'AAAAAAAA': 10})

    def test_the_spool_has_its_own_entry_limit(self):
        for index in range(3):
            self.media.write(f'file{index}', 2)
        self.assertEqual(directory_usage(self.media.root).truncated, False)
        partial = directory_usage(self.media.root, entries=2)
        self.assertEqual((partial.files, partial.size, partial.truncated), (2, 4, True))


class ClassifyTests(SimpleTestCase):
    def test_categories(self):
        shas = frozenset({'ABCDEF12'})
        cases = {
            MediaFile('ABCDEF12', 1): Category.NETWORKS,
            MediaFile('PGNs/12.pgn.tar', 1): Category.PGN_ARCHIVES,
            MediaFile('12.3.16.pgn.bz2', 1): Category.PGN_PENDING,
            MediaFile('event4.log', 1): Category.EVENT_LOGS,
            MediaFile('event4_Xy12AbC.log', 1): Category.EVENT_LOGS,
            MediaFile('12345678', 1): Category.OTHER,
            MediaFile('PGNs/', 1, is_dir=True): Category.OTHER,
            MediaFile('PGNs/notes.txt', 1): Category.OTHER,
        }
        for file, category in cases.items():
            with self.subTest(file=file.path):
                self.assertEqual(classify(file, shas).category, category)

    def test_unreferenced_network_names_are_flagged(self):
        self.assertTrue(classify(MediaFile('12345678', 1), frozenset()).unreferenced_network)
        self.assertFalse(classify(MediaFile('notes.txt', 1), frozenset()).unreferenced_network)


class DiskUsageTests(SimpleTestCase):
    def test_low_below_the_fraction_or_the_absolute_floor(self):
        self.assertFalse(DiskUsage(total=100 * GIB, used=80 * GIB, free=20 * GIB).low)
        self.assertTrue(DiskUsage(total=100 * GIB, used=90 * GIB, free=10 * GIB).low)
        self.assertTrue(DiskUsage(total=4 * GIB, used=int(2.5 * GIB), free=int(1.5 * GIB)).low)

    def test_bytes_are_formatted_in_binary_units(self):
        self.assertEqual(
            [format_bytes(size) for size in (0, 1023, 1536, 5 * GIB)],
            ['0 B', '1023 B', '1.5 KiB', '5.0 GiB'],
        )


class ReportTests(TestCase):
    def setUp(self):
        self.media = MediaTree(self)
        create_engine_config('Avalanche')
        create_engine_config('Torch')
        ensure_book()
        self.author = create_user('author')

    def report(self, count: int = 10, walker: Walker | None = None):
        return build_report(
            self.media.root,
            self.media.root.parent,
            self.media.root.parent / 'db.sqlite3',
            count=count,
            walker=walker,
        )

    def test_networks_are_counted_per_engine_and_shared_files_once(self):
        self.media.write('AAAAAAAA', 100)
        self.media.write('BBBBBBBB', 40)
        Network.objects.create(engine='Avalanche', name='net-a', sha256='AAAAAAAA', author='author')
        Network.objects.create(engine='Torch', name='shared', sha256='AAAAAAAA', author='author')
        Network.objects.create(engine='Torch', name='net-b', sha256='BBBBBBBB', author='author')
        Network.objects.create(engine='Torch', name='gone', sha256='CCCCCCCC', author='author')

        report = self.report()
        networks = report.category(Category.NETWORKS)
        self.assertEqual((networks.files, networks.size), (2, 140))
        engines = {usage.engine: usage for usage in report.networks_by_engine}
        self.assertEqual(
            (
                engines['Torch'].networks,
                engines['Torch'].files,
                engines['Torch'].missing,
                engines['Torch'].size,
            ),
            (3, 2, 1, 140),
        )
        self.assertEqual(
            (engines['Avalanche'].size, engines['Avalanche'].url),
            (100, '/networks/Avalanche/'),
        )
        largest = networks.largest[0]
        self.assertEqual(
            (largest.name, largest.detail),
            ('AAAAAAAA', 'Avalanche / net-a, Torch / shared'),
        )

    def test_network_presence_does_not_depend_on_the_scan_limit(self):
        for sha in ('AAAAAAAA', 'BBBBBBBB', 'CCCCCCCC'):
            self.media.write(sha, 10)
            Network.objects.create(engine='Avalanche', name=sha.lower(), sha256=sha, author='author')
        report = self.report(walker=Walker(entries=1))
        self.assertTrue(report.truncated)
        (engine,) = report.networks_by_engine
        self.assertEqual((engine.files, engine.missing, engine.size), (3, 0, 30))

    def test_networks_of_unconfigured_engines_are_not_linked(self):
        self.media.write('AAAAAAAA', 10)
        Network.objects.create(engine='Retired', name='old', sha256='AAAAAAAA', author='author')
        report = self.report()
        (engine,) = report.networks_by_engine
        self.assertEqual((engine.engine, engine.url), ('Retired', None))
        self.assertIsNone(report.category(Category.NETWORKS).largest[0].url)

    def test_pgn_files_link_to_their_workload(self):
        test = create_test(self.author)
        tune = create_test(self.author, test_mode='SPSA')
        self.media.write(f'PGNs/{test.id}.pgn.tar', 500)
        self.media.write(f'PGNs/{tune.id}.pgn.tar', 300)
        self.media.write('PGNs/99999.pgn.tar', 200)
        self.media.write(f'{test.id}.4.0.pgn.bz2', 7)

        report = self.report()
        archives = report.category(Category.PGN_ARCHIVES).largest
        self.assertEqual(
            [item.url for item in archives],
            [f'/test/{test.id}/', f'/tune/{tune.id}/', None],
        )
        self.assertIn('workload deleted', archives[2].detail)
        pending = report.category(Category.PGN_PENDING)
        self.assertEqual((pending.files, pending.largest[0].url), (1, f'/test/{test.id}/'))

    def test_event_logs_link_to_their_event(self):
        event = LogEvent.objects.create(author='w', summary='Bench mismatch', log_file='event1.log')
        self.media.write('event1.log', 64)
        self.media.write('event2.log', 8)
        logs = self.report().category(Category.EVENT_LOGS).largest
        self.assertEqual((logs[0].url, logs[0].detail), (f'/event/{event.id}/', 'Bench mismatch'))
        self.assertEqual((logs[1].url, logs[1].detail), (None, 'No event references this log'))

    def test_largest_items_are_bounded_and_ordered(self):
        for size in (5, 50, 20, 40):
            self.media.write(f'misc-{size}', size)
        self.media.write('DEADBEEF', 1)
        report = self.report(count=3)
        other = report.category(Category.OTHER)
        self.assertEqual((other.files, other.size), (5, 116))
        self.assertEqual([item.size for item in other.largest], [50, 40, 20])
        self.assertEqual(report.media_size, 116)

    def test_every_category_is_reported_even_when_empty(self):
        report = self.report()
        self.assertEqual([usage.category for usage in report.categories], list(Category))
        self.assertEqual(report.media_size, 0)


class CachedReportTests(TestCase):
    def setUp(self):
        self.media = MediaTree(self)
        self.enterContext(override_settings(MEDIA_ROOT=str(self.media.root), FILE_UPLOAD_TEMP_DIR=None))
        cache.delete(CACHE_KEY)
        self.addCleanup(cache.delete, CACHE_KEY)

    def test_the_report_is_cached_between_calls(self):
        self.media.write('first', 10)
        self.assertEqual(current_report().media_size, 10)
        self.media.write('second', 10)
        self.assertEqual(current_report().media_size, 10)
        cache.delete(CACHE_KEY)
        self.assertEqual(current_report().media_size, 20)

    def test_the_upload_spool_is_measured_when_configured(self):
        spool = self.media.root.parent / 'upload-tmp'
        spool.mkdir()
        (spool / 'partial.upload').write_bytes(b'x' * 25)
        with override_settings(FILE_UPLOAD_TEMP_DIR=str(spool)):
            spool_usage = current_report().upload_spool
        self.assertEqual((spool_usage.files, spool_usage.size, spool_usage.truncated), (1, 25, False))


def create_manager(username):
    user = create_user(username)
    Profile.objects.filter(user=user).update(superuser=True)
    return user


class StorageViewTests(TestCase):
    def setUp(self):
        self.media = MediaTree(self)
        self.enterContext(override_settings(MEDIA_ROOT=str(self.media.root), FILE_UPLOAD_TEMP_DIR=None))
        cache.delete(CACHE_KEY)
        self.addCleanup(cache.delete, CACHE_KEY)
        self.disk = self.enterContext(
            mock.patch(
                'OpenBench.storage.report.disk_usage',
                return_value=DiskUsage(total=100 * GIB, used=50 * GIB, free=50 * GIB),
            )
        )
        create_engine_config('Avalanche')
        self.user = create_manager('viewer')
        self.media.write('AAAAAAAA', 2048)
        Network.objects.create(engine='Avalanche', name='net-a', sha256='AAAAAAAA', author='viewer')

    def test_anonymous_visitors_are_sent_to_login(self):
        response = self.client.get('/manage/storage/')
        self.assertRedirects(response, '/login/', fetch_redirect_response=False)

    def test_accounts_not_yet_enabled_are_refused(self):
        self.client.force_login(create_user('pending', enabled=False))
        response = self.client.get('/manage/storage/')
        self.assertRedirects(response, '/index/', fetch_redirect_response=False)

    def test_enabled_users_who_do_not_manage_are_refused(self):
        self.client.force_login(create_user('worker'))
        response = self.client.get('/manage/storage/')
        self.assertRedirects(response, '/manage/books/', fetch_redirect_response=False)
        self.assertNotContains(self.client.get('/manage/books/'), 'href="/manage/storage/"')

    def test_managers_see_the_overview(self):
        self.client.force_login(self.user)
        response = self.client.get('/manage/storage/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'href="/manage/storage/"')
        self.assertContains(response, 'Pending PGN batches')
        self.assertContains(response, '<a href="/networks/Avalanche/">Avalanche / net-a</a>', html=True)
        self.assertContains(response, '2.0 KiB')
        self.assertNotContains(response, 'Disk space is low')

    def test_low_disk_space_is_flagged(self):
        self.disk.return_value = DiskUsage(total=100 * GIB, used=99 * GIB, free=GIB)
        self.client.force_login(self.user)
        response = self.client.get('/manage/storage/')
        self.assertContains(response, 'Disk space is low')
        self.assertContains(response, 'stat-tile-warn')

    def test_the_page_has_no_state_changing_forms(self):
        self.client.force_login(self.user)
        content = self.client.get('/manage/storage/').content.decode().split('id="content"', 1)[1]
        self.assertNotIn('<form', content)

    def test_the_page_model_matches_the_report(self):
        page = storage_page(current_report())
        self.assertEqual(
            [tile.label for tile in page.tiles],
            ['Disk free', 'Database', 'Media', 'Networks'],
        )
        self.assertEqual(page.tiles[3].value, '2.0 KiB')
        self.assertEqual(page.tiles[3].meta, '1 network, 1 file, 1 engine')


class StorageApiTests(TestCase):
    def setUp(self):
        self.media = MediaTree(self)
        self.enterContext(override_settings(MEDIA_ROOT=str(self.media.root), FILE_UPLOAD_TEMP_DIR=None))
        cache.delete(CACHE_KEY)
        self.addCleanup(cache.delete, CACHE_KEY)
        self.user = create_manager('viewer')
        self.media.write('event3.log', 12)

    def test_anonymous_requests_are_refused(self):
        response = self.client.get('/api/storage/')
        self.assertEqual(response.status_code, 401)

    def test_disabled_users_are_refused(self):
        self.client.force_login(create_user('disabled', enabled=False))
        self.assertEqual(self.client.get('/api/storage/').status_code, 401)

    def test_enabled_users_who_do_not_manage_are_forbidden(self):
        self.client.force_login(create_user('worker'))
        response = self.client.get('/api/storage/')
        self.assertEqual(response.status_code, 403)
        self.assertNotIn('storage', json.loads(response.content))

    def test_django_superusers_are_managers(self):
        user = create_user('root')
        user.is_superuser = True
        user.save()
        self.client.force_login(user)
        self.assertEqual(self.client.get('/api/storage/').status_code, 200)

    def test_session_users_get_the_documented_schema(self):
        self.client.force_login(self.user)
        payload = json.loads(self.client.get('/api/storage/').content)['storage']
        self.assertEqual(
            set(payload),
            {
                'generated_at',
                'cache_seconds',
                'disk',
                'database',
                'media',
                'networks_by_engine',
                'upload_spool',
            },
        )
        self.assertEqual(
            set(payload['disk']),
            {'total_bytes', 'used_bytes', 'free_bytes', 'free_fraction', 'low'},
        )
        self.assertEqual(set(payload['database']), {'bytes', 'main_bytes', 'wal_bytes', 'shm_bytes'})
        self.assertEqual(payload['media']['bytes'], 12)
        categories = {category['key']: category for category in payload['media']['categories']}
        self.assertEqual(list(categories), [category.value for category in Category])
        self.assertEqual(
            categories['event_logs']['largest'],
            [
                {
                    'name': 'event3.log',
                    'bytes': 12,
                    'detail': 'No event references this log',
                    'url': None,
                }
            ],
        )
        self.assertEqual(
            set(payload['media']),
            {
                'files',
                'bytes',
                'skipped_symlinks',
                'unreadable_dirs',
                'truncated',
                'categories',
            },
        )
        self.assertIsNone(payload['upload_spool'])

    def test_credentials_in_the_body_are_accepted(self):
        response = self.client.post('/api/storage/', credentials(self.user))
        self.assertEqual(response.status_code, 200)
        self.assertIn('storage', json.loads(response.content))

    def test_the_media_root_is_read_at_request_time(self):
        self.client.force_login(self.user)
        os.remove(self.media.root / 'event3.log')
        payload = json.loads(self.client.get('/api/storage/').content)['storage']
        self.assertEqual(payload['media']['files'], 0)
