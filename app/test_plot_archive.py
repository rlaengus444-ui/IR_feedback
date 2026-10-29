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

class ArchiveComponentTests(unittest.TestCase):
    def test_png_failure_preserves_completed_and_retries_only_missing(self):
        from plot_images import write_plot
        with tempfile.TemporaryDirectory() as d:
            counts = {'csv': 0, 'temperature': 0, 'ir': 0}
            def csv_writer(path, points):
                counts['csv'] += 1
                _write_csv(path, points)
            def image_writer(path, points, kind, target):
                counts[kind] += 1
                if kind == 'temperature' and counts[kind] == 1:
                    path.write_bytes(b'incomplete')
                    raise OSError('png failure')
                write_plot(path, points, kind, target)
            archive = PlotArchive(directories=[d], writer=csv_writer, image_writer=image_writer)
            self.addCleanup(archive.close)
            archive.capture((3, 4), [point(1), point(2)], target_temperature=75)
            archive.submit()
            error = wait(archive)[0]
            self.assertEqual('error', error['kind'])
            self.assertIsNone(error['plot_paths']['temperature'])
            csv_path = Path(error['path'])
            ir_path = Path(error['plot_paths']['ir'])
            self.assertTrue(csv_path.exists())
            self.assertTrue(ir_path.exists())
            self.assertEqual(2, archive.retained_rows)
            self.assertEqual([], list(Path(d).glob('.ir-plot-*')))
            csv_bytes, ir_bytes = csv_path.read_bytes(), ir_path.read_bytes()
            retry = Path(d) / 'retry'
            archive.submit(retry_failed=True, directory=retry)
            result = wait(archive)[0]
            self.assertEqual('saved', result['kind'])
            self.assertEqual({'csv': 1, 'temperature': 2, 'ir': 1}, counts)
            self.assertEqual(str(csv_path), result['path'])
            self.assertEqual(str(ir_path), result['plot_paths']['ir'])
            self.assertEqual(retry, Path(result['plot_paths']['temperature']).parent)
            self.assertEqual(csv_bytes, csv_path.read_bytes())
            self.assertEqual(ir_bytes, ir_path.read_bytes())
            self.assertEqual(0, archive.retained_rows)

    def test_ir_failure_keeps_temperature_and_csv(self):
        from plot_images import write_plot
        def writer(path, points, kind, target):
            if kind == 'ir':
                raise OSError('ir failed')
            write_plot(path, points, kind, target)
        with tempfile.TemporaryDirectory() as d:
            archive = PlotArchive(directories=[d], image_writer=writer)
            self.addCleanup(archive.close)
            archive.capture((1, 5), [point(1)])
            archive.submit()
            event = wait(archive)[0]
            self.assertEqual('error', event['kind'])
            self.assertTrue(Path(event['path']).exists())
            self.assertTrue(Path(event['plot_paths']['temperature']).exists())
            self.assertIsNone(event['plot_paths']['ir'])
            self.assertEqual(1, archive.retained_rows)

    def test_existing_png_is_preserved_and_unique_path_selected(self):
        with tempfile.TemporaryDirectory() as d:
            collision = []
            def writer(path, points):
                _write_csv(path, points)
                existing = path.with_name(path.stem + '_temperature.png')
                existing.write_bytes(b'existing')
                collision.append(existing)
            archive = PlotArchive(directories=[d], writer=writer)
            self.addCleanup(archive.close)
            archive.capture((8, 1), [point(1)])
            archive.submit()
            result = wait(archive)[0]
            self.assertEqual('saved', result['kind'])
            self.assertNotEqual(str(collision[0]), result['plot_paths']['temperature'])
            self.assertEqual(b'existing', collision[0].read_bytes())
            self.assertEqual(Path(result['path']).stem + '_ir.png', Path(result['plot_paths']['ir']).name)

    def test_png_fallback_keeps_csv_and_reports_separate_folder(self):
        from plot_images import write_plot
        with tempfile.TemporaryDirectory() as d:
            first, second = Path(d) / 'first', Path(d) / 'second'
            def writer(path, points, kind, target):
                if path.parent == first:
                    raise OSError('folder rejects images')
                write_plot(path, points, kind, target)
            archive = PlotArchive(directories=[first, second], image_writer=writer)
            self.addCleanup(archive.close)
            archive.capture((8, 2), [point(1)])
            archive.submit()
            result = wait(archive)[0]
            self.assertEqual('saved', result['kind'])
            self.assertEqual(first, Path(result['path']).parent)
            self.assertEqual({second}, {Path(p).parent for p in result['plot_paths'].values()})
            self.assertEqual([], list(first.glob('*.png')))
