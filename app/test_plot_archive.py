import csv
from pathlib import Path
import tempfile
import threading
import time
import unittest

from layer_charts import LayerPoint, _write_csv
from plot_archive import PlotArchive


def point(i):
    return LayerPoint(i, 40.5, 60, 61, 62, 63, i % 2 == 0)


def wait(archive, n=1):
    events = []
    deadline = time.monotonic() + 5
    while len(events) < n and time.monotonic() < deadline:
        events.extend(archive.poll())
        if len(events) < n:
            time.sleep(.005)
    if len(events) != n:
        raise AssertionError(events)
    return events


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.archives = []
        self.addCleanup(lambda: [archive.close() for archive in self.archives])

    def archive(self, **kwargs):
        archive = PlotArchive(directories=kwargs.pop('directories', [self.root]), **kwargs)
        self.archives.append(archive)
        return archive

    def test_all_rows_and_fields_with_bom(self):
        archive = self.archive()
        points = [point(i) for i in range(2501)]
        self.assertTrue(archive.capture((1, 1), points))
        points.clear()
        archive.submit()
        event = wait(archive)[0]
        self.assertEqual(('saved', (1, 1), 2501), (event['kind'], event['key'], event['row_count']))
        path = Path(event['path'])
        self.assertTrue(path.read_bytes().startswith(b'\xef\xbb\xbf'))
        with path.open(encoding='utf-8-sig', newline='') as stream:
            rows = list(csv.reader(stream))
        self.assertEqual(2502, len(rows))
        self.assertTrue(all(len(row) == 7 for row in rows))
        self.assertEqual(['1', '40.5', '60', '61', '62', '63', '아니오'], rows[2])
        self.assertEqual(0, archive.retained_rows)
        self.assertFalse(archive.busy)

    def test_fallback_unique_files_preserve_existing(self):
        blocked = self.root / 'file'
        blocked.write_text('keep')
        output = self.root / 'fallback'
        output.mkdir()
        existing = output / 'existing.csv'
        existing.write_text('preserve')
        archive = self.archive(directories=[blocked, output])
        archive.capture((1, 1), [point(1)])
        archive.capture((1, 2), [point(2)])
        archive.submit()
        events = wait(archive, 2)
        self.assertTrue(all(e['kind'] == 'saved' and Path(e['path']).parent == output for e in events))
        self.assertEqual(2, len({e['path'] for e in events}))
        self.assertEqual('preserve', existing.read_text())

    def test_failed_partial_write_retained_then_explicit_retry(self):
        attempts = []
        def writer(path, points):
            attempts.append(path)
            if len(attempts) == 1:
                path.write_text('partial')
                raise OSError('disk full')
            _write_csv(path, points)
        archive = self.archive(writer=writer)
        archive.capture((2, 1), [point(1), point(2)])
        archive.submit()
        event = wait(archive)[0]
        self.assertEqual('error', event['kind'])
        self.assertIn('disk full', event['error'])
        self.assertFalse(attempts[0].exists())
        self.assertEqual(2, archive.retained_rows)
        self.assertTrue(archive.failed)
        self.assertFalse(archive.capture((2, 1), [point(3)]))
        archive.submit()
        self.assertFalse(archive.busy)
        retry = self.root / 'retry'
        archive.submit(retry_failed=True, directory=retry)
        event = wait(archive)[0]
        self.assertEqual('saved', event['kind'])
        self.assertEqual(retry, Path(event['path']).parent)
        self.assertFalse(archive.failed)
        self.assertEqual(0, archive.retained_rows)

    def test_empty_duplicates_and_rapid_segments(self):
        archive = self.archive()
        self.assertFalse(archive.capture((1, 0), []))
        keys = ((1, 1), (1, 2), (1, 3), (2, 1))
        for key in keys:
            self.assertTrue(archive.capture(key, [point(1)]))
            self.assertFalse(archive.capture(key, [point(2)]))
        archive.submit()
        archive.submit()
        self.assertEqual(set(keys), {e['key'] for e in wait(archive, 4)})
        self.assertFalse(archive.capture((1, 1), [point(3)]))
        archive.submit(retry_failed=True)
        self.assertEqual([], archive.poll())

    def test_blocked_worker_does_not_block_capture_or_submit(self):
        entered, release = threading.Event(), threading.Event()
        def writer(path, points):
            entered.set()
            if not release.wait(5):
                raise RuntimeError('writer timed out')
            _write_csv(path, points)
        self.addCleanup(release.set)
        archive = self.archive(writer=writer)
        archive.capture((1, 1), [point(1)])
        archive.submit()
        self.assertTrue(entered.wait(2))
        start = time.monotonic()
        self.assertTrue(archive.capture((1, 2), [point(2)]))
        archive.submit()
        archive.submit()
        self.assertLess(time.monotonic() - start, .5)
        self.assertTrue(archive.busy)
        self.assertEqual(2, archive.retained_rows)
        release.set()
        self.assertEqual({(1, 1), (1, 2)}, {e['key'] for e in wait(archive, 2)})

    def test_worker_start_failure_retains_data_for_a_later_retry(self):
        def failed_thread(**kwargs):
            raise RuntimeError('thread unavailable')
        archive = self.archive(thread_factory=failed_thread)
        archive.capture((1, 1), [point(1)])
        archive.submit()
        event = wait(archive)[0]
        self.assertEqual('error', event['kind'])
        self.assertEqual(1, archive.retained_rows)
        self.assertFalse(archive.busy)
        self.assertEqual([], list(self.root.glob('*.csv')))
        archive.thread_factory = threading.Thread
        archive.submit(retry_failed=True)
        self.assertEqual('saved', wait(archive)[0]['kind'])
        self.assertEqual(0, archive.retained_rows)


if __name__ == '__main__':
    unittest.main()
