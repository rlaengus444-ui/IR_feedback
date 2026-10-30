"""Offline Tk lifecycle tests for automatic chart archives."""
import csv
from dataclasses import replace
from pathlib import Path
import tempfile
import threading
import time
import tkinter as tk
import unittest
from unittest.mock import patch
from PIL import Image

from layer_charts import _write_csv
from plot_archive import PlotArchive
from session import SessionController, WorkerEvent
from standalone_ir import ThermalFeedbackWindow
from test_session import DeferredThread, TARGET, snapshot


class ArchiveViewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = tk.Tk()
        self.root.withdraw()
        self.controller = SessionController(lambda *args: None, DeferredThread)
        self.archive = PlotArchive(directories=[self.temp.name])
        self.view = ThermalFeedbackWindow(self.root, controller=self.controller,
            discoverer=lambda: [], auto_discover=False, archive=self.archive)
        self.view.targets = [TARGET]
        self.view._populate_hosts()
        self.error_patch = patch('standalone_ir.write_error_log')
        self.error_patch.start()
        self.addCleanup(self.error_patch.stop)
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.archive.close()
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def connect(self):
        self.view.connect()
        self.assertEqual('connecting', self.controller.state)
        self.emit('connected', {})
        self.view._pump()

    def emit(self, kind, payload, generation=None):
        self.controller.events.put(WorkerEvent(self.controller.generation if generation is None else generation, kind, payload))

    def data(self, i, run='A', saved=None):
        return {'snapshot': replace(snapshot(f'{run}-{i}', i), run_key=run, coupled_output=True),
                'applied': {} if saved is None else {1: saved}, 'proposed': {1: 99},
                'effective_mode': 'control', 'reasons': {1: 'proposal'}, 'status': 'proposal'}

    def test_retired_run_png_uses_outgoing_target_and_both_plots_exist(self):
        self.connect()
        first, second = self.data(1, 'A'), self.data(2, 'B')
        first['display_target_c'], second['display_target_c'] = 65, 75
        self.emit('snapshot', first)
        self.emit('snapshot', second)
        self.emit('finished', None)
        self.view._pump()
        end = time.monotonic() + 5
        while self.archive.busy and time.monotonic() < end:
            self.view._pump()
            time.sleep(.005)
        self.assertFalse(self.archive.busy)
        pngs = list(Path(self.temp.name).glob('*.png'))
        self.assertEqual(4, len(pngs))
        targets = []
        for path in pngs:
            with Image.open(path) as image:
                targets.append(image.info['target_at_capture'])
        self.assertEqual(['65', '65', '75', '75'], sorted(targets))
        self.assertEqual(2, len(list(Path(self.temp.name).glob('*.csv'))))

    def test_settings_hold_keeps_graph_connection_until_explicit_finish(self):
        self.connect()
        data = self.data(1)
        data['snapshot'] = replace(data['snapshot'], control_available=False,
            control_recoverable=True, control_unavailable_reason='pending edit')
        data['effective_mode'] = 'control-waiting'
        data['proposed'] = {}
        self.emit('snapshot', data)
        self.view._pump()
        self.assertEqual('connected', self.controller.state)
        self.assertIn('연결됨', self.view.status.get())
        self.assertEqual(1, len(self.view.charts.history.points))
        self.assertEqual([], list(Path(self.temp.name).iterdir()))
        self.emit('finished', None)
        self.view._pump()
        end = time.monotonic() + 5
        while self.archive.busy and time.monotonic() < end:
            self.view._pump()
            time.sleep(.005)
        self.assertEqual(1, len(list(Path(self.temp.name).glob('*.csv'))))
        self.assertEqual(2, len(list(Path(self.temp.name).glob('*.png'))))

    def test_csv_wait_keeps_pump_alive_and_delivers_later_path_binding(self):
        from windows_slice_source import PathBinding, SourceSelection
        self.connect()
        data = self.data(255)
        data.update(effective_mode='observation-waiting', proposed={}, reasons={},
                    status='연결 유지 · CSV 갱신 대기')
        self.emit('snapshot', data)
        with patch.object(self.root, 'after', wraps=self.root.after) as schedule:
            self.view._pump()
        self.assertTrue(any(c.args == (50, self.view._pump) for c in schedule.call_args_list))
        self.assertEqual('connected', self.controller.state)
        self.assertIn('CSV', self.view.status.get())
        self.assertEqual(1, len(self.view.charts.history.points))
        binding = PathBinding(TARGET, 101, 201, (), 'Home')
        selection = SourceSelection('C:\\slices', 'C:\\slices', binding)
        self.view._binding_serial = 1
        self.view._binding_queue.put((1, self.controller.generation, TARGET, binding, selection, None))
        with patch('standalone_ir.messagebox.askyesno', return_value=True) as confirm:
            self.view._pump()
        confirm.assert_called_once()
        self.assertIs(binding, self.view._source_binding)
        self.assertEqual([], list(Path(self.temp.name).iterdir()))

    def pump_until(self, predicate):
        deadline = time.monotonic() + 5
        while not predicate() and time.monotonic() < deadline:
            self.view._pump()
            time.sleep(.005)
        self.assertTrue(predicate())

    def files(self):
        return list(Path(self.temp.name).glob('*.csv'))

    def rows(self, path):
        with path.open(encoding='utf-8-sig', newline='') as stream:
            return list(csv.reader(stream))

    def test_disconnect_drains_batches_then_finished_archives_full_history_once(self):
        self.connect()
        for start in range(0, 2001, 64):
            for i in range(start, min(start + 64, 2001)):
                self.emit('snapshot', self.data(i))
            self.view._pump()
        self.view.disconnect()
        for i in range(2001, 2071):
            self.emit('snapshot', self.data(i, saved=66 if i == 2070 else None))
        self.view._pump()
        self.assertEqual([], self.files())
        self.view._pump()
        self.assertEqual([], self.files())
        self.emit('finished', None)
        self.view._pump()
        self.pump_until(lambda: not self.archive.busy)
        self.assertEqual(1, len(self.files()))
        rows = self.rows(self.files()[0])
        self.assertEqual(2072, len(rows))
        self.assertEqual('66.0', rows[-1][4])
        self.emit('finished', None)
        self.view._pump()
        self.assertEqual(1, len(self.files()))

    def test_error_does_not_archive_before_finished(self):
        self.connect()
        self.emit('snapshot', self.data(1))
        self.emit('error', 'fixture error')
        self.view._pump()
        self.assertEqual([], self.files())
        self.emit('finished', None)
        self.view._pump()
        self.pump_until(lambda: not self.archive.busy)
        self.assertEqual(1, len(self.files()))

    def test_run_a_b_a_yields_distinct_segments(self):
        self.connect()
        for i, run in enumerate(('A', 'B', 'A')):
            self.emit('snapshot', self.data(i, run))
        self.view._pump()
        self.emit('finished', None)
        self.view._pump()
        self.pump_until(lambda: not self.archive.busy)
        self.assertEqual(3, len(self.files()))
        self.assertEqual({2}, {len(self.rows(path)) for path in self.files()})

    def test_empty_finished_has_no_archive(self):
        self.connect()
        self.emit('finished', None)
        self.view._pump()
        self.assertFalse(self.archive.busy)
        self.assertEqual([], self.files())

    def test_disconnect_chart_only_and_old_generation_events(self):
        self.connect()
        with self.assertRaises(ValueError):
            self.controller.drain_chart_events()
        self.view.disconnect()
        self.emit('snapshot', self.data(5, saved=57))
        self.emit('snapshot', self.data(6), generation=self.controller.generation - 1)
        events = self.controller.poll()
        self.assertEqual(1, len(events))
        self.assertEqual('chart_snapshot', events[0].kind)
        self.assertEqual({'snapshot', 'applied'}, set(events[0].payload))
        self.assertEqual('disconnecting', self.controller.state)
        self.view._record_chart_payload(events[0].payload)
        self.assertEqual(57, self.view.charts.history.points[0].ir1_saved)
        self.assertEqual(1, len(self.view.charts.history.points))

    def test_close_waits_for_all_retained_chart_event_batches(self):
        self.connect()
        for i in range(70):
            self.emit('snapshot', self.data(i))
        self.emit('finished', None)
        self.controller.thread.alive = False
        self.view.close()
        self.view._pump()
        self.assertFalse(self.view._closing_archive_started)
        self.assertEqual([], self.files())
        self.view._pump()
        self.assertTrue(self.view._closing_archive_started)
        self.pump_until(lambda: not self.archive.busy)
        self.assertEqual(71, len(self.rows(self.files()[0])))

    def test_close_deadline_drains_confirmed_saves_and_ignores_proposals(self):
        self.connect()
        self.view.close()
        self.emit('snapshot', self.data(4, saved=58))
        self.emit('snapshot', self.data(5))
        self.emit('snapshot', self.data(6), generation=self.controller.generation - 1)
        self.view.close_deadline = time.monotonic() - 1
        self.view._pump()
        self.pump_until(lambda: not self.archive.busy)
        rows = self.rows(self.files()[0])
        self.assertEqual(3, len(rows))
        self.assertEqual('58.0', rows[1][4])
        self.assertEqual('', rows[2][4])
        self.assertTrue(self.controller.closed)
        self.assertEqual('disconnecting', self.controller.state)

    def test_pending_export_does_not_block_reconnect_or_close(self):
        entered, release = threading.Event(), threading.Event()
        def writer(path, points):
            entered.set()
            if not release.wait(5):
                raise RuntimeError('writer timed out')
            _write_csv(path, points)
        self.addCleanup(release.set)
        self.archive.writer = writer
        self.connect()
        self.emit('snapshot', self.data(1))
        self.emit('finished', None)
        self.view._pump()
        self.assertTrue(entered.wait(2))
        self.connect()
        self.emit('snapshot', self.data(2, 'B'))
        self.emit('finished', None)
        self.view._pump()
        start = time.monotonic()
        self.view.close()
        self.view._pump()
        self.assertLess(time.monotonic() - start, .5)
        self.assertTrue(self.archive.busy)
        self.assertTrue(self.root.winfo_exists())
        release.set()
        self.pump_until(lambda: not self.archive.busy)
        self.assertEqual(2, len(self.files()))

    def test_failed_archive_survives_reconnect_and_cancel_then_retry(self):
        def broken(path, points):
            raise OSError('disk failure')
        self.archive.writer = broken
        self.connect()
        self.emit('snapshot', self.data(1))
        self.emit('finished', None)
        self.view._pump()
        self.pump_until(lambda: not self.archive.busy)
        self.assertTrue(self.archive.failed)
        self.connect()
        self.emit('snapshot', self.data(2, 'B'))
        self.emit('finished', None)
        self.view._pump()
        self.pump_until(lambda: not self.archive.busy)
        self.assertGreaterEqual(self.archive.retained_rows, 2)
        self.view.close()
        self.view.close_deadline = time.monotonic() - 1
        with patch('standalone_ir.filedialog.askdirectory', return_value=''):
            self.view._pump()
            self.pump_until(lambda: not self.view.closing)
        self.assertTrue(self.root.winfo_exists())
        self.assertTrue(self.controller.closed)
        self.assertGreaterEqual(self.archive.retained_rows, 2)
        alternate = Path(self.temp.name) / 'alternate'
        def alternate_only(path, points):
            if path.parent != alternate:
                raise OSError('disk failure')
            _write_csv(path, points)
        self.archive.writer = alternate_only
        self.view.close()
        with patch('standalone_ir.filedialog.askdirectory', return_value=str(alternate)):
            self.pump_until(lambda: not self.archive.busy and self.archive.retained_rows == 0)
        self.assertEqual(2, len(list(alternate.glob('*.csv'))))

    def test_failed_close_retry_keeps_original_cutoff_after_late_snapshot(self):
        def broken(path, points):
            raise OSError('disk failure')
        self.archive.writer = broken
        self.connect()
        self.emit('snapshot', self.data(1))
        self.view._pump()
        self.view.close()
        self.view.close_deadline = time.monotonic() - 1
        with patch('standalone_ir.filedialog.askdirectory', return_value=''):
            self.pump_until(lambda: not self.view.closing)
        self.assertTrue(self.view._closing_charts_finalized)
        self.emit('snapshot', self.data(2, saved=61))
        self.view._pump()
        self.archive.writer = _write_csv
        self.view.close()
        self.pump_until(lambda: not self.archive.busy and self.archive.retained_rows == 0)
        rows = self.rows(self.files()[0])
        self.assertEqual(['2'], [row[0] for row in rows[1:]])
        self.assertEqual([2], [point.layer for point in self.view.charts.history.points])
        self.assertEqual('', rows[1][4])
        self.assertTrue(self.controller.closed)


if __name__ == '__main__':
    unittest.main()
