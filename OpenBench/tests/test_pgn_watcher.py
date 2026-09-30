import os
import tarfile
import tempfile
import threading

from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage
from django.test import TestCase, override_settings

from OpenBench.models import PGN
from OpenBench.pgn_watcher import PGNWatcher

class PGNWatcherTests(TestCase):

    def setUp(self):
        self.media = tempfile.TemporaryDirectory()
        self.enterContext(override_settings(MEDIA_ROOT=self.media.name))
        self.addCleanup(self.media.cleanup)
        self.watcher = PGNWatcher(threading.Event())

    def upload(self, test_id, book_index, save_file=True):
        pgn = PGN.objects.create(test_id=test_id, result_id=1, book_index=book_index)
        if save_file:
            FileSystemStorage().save(pgn.filename(), ContentFile(b'pgn'))
        return pgn

    def archive(self, test_id):
        path = os.path.join(self.media.name, 'PGNs', '%d.pgn.tar' % (test_id))
        with tarfile.open(path) as tar:
            return sorted(tar.getnames())

    def test_pgns_are_archived_per_test(self):
        self.upload(1, 0)
        self.upload(1, 16)
        self.upload(2, 0)
        self.assertEqual(self.watcher.process_pending(), 3)
        self.assertEqual(self.archive(1), ['1.1.0.pgn.bz2', '1.1.16.pgn.bz2'])
        self.assertEqual(self.archive(2), ['2.1.0.pgn.bz2'])
        self.assertFalse(PGN.objects.filter(processed=False).exists())
        self.assertFalse(os.path.exists(os.path.join(self.media.name, '1.1.0.pgn.bz2')))

    def test_later_uploads_are_appended(self):
        self.upload(1, 0)
        self.watcher.process_pending()
        self.upload(1, 16)
        self.watcher.process_pending()
        self.assertEqual(self.archive(1), ['1.1.0.pgn.bz2', '1.1.16.pgn.bz2'])

    def test_a_missing_file_does_not_block_other_pgns(self):
        missing = self.upload(1, 0, save_file=False)
        present = self.upload(1, 16)
        other   = self.upload(2, 0)
        self.watcher.process_pending()
        self.assertEqual(self.archive(1), ['1.1.16.pgn.bz2'])
        self.assertEqual(self.archive(2), ['2.1.0.pgn.bz2'])
        self.assertEqual(list(PGN.objects.filter(processed=False)), [missing])
        for pgn in (present, other):
            pgn.refresh_from_db()
            self.assertTrue(pgn.processed)
