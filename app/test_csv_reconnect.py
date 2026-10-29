"""Offline regression coverage for resuming within an existing datasheet."""
import csv
from datetime import datetime, timedelta
import io
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from adapter_contracts import ControlUnavailable, MachineWindow, UiSnapshot
from csv_source import CsvSourceError, JobContext, MonitoringCsvReader
from session import SessionOptions, run_ui_session


def encode(rows):
    stream = io.StringIO(newline="")
    csv.writer(stream, lineterminator="\r\n").writerows(rows)
    return stream.getvalue().encode()


class ReconnectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.now = datetime.now().replace(microsecond=0)
        self.started = self.now - timedelta(minutes=3)
        self.target = MachineWindow("M1", 1, 2, "fixture", str(self.root / "Operator.exe"))
        logs = self.root / "Logs"
        logs.mkdir()
        stamp = lambda t: t.strftime("%Y-%m-%d%p%I%M%S")
        (logs / "job_M1.log").write_text(
            f'{stamp(self.started)}...[BJT - AUTO]: AutoPrint started!\n'
            f'{stamp(self.started)}...[BJT - AUTO]: Layer: 1058 -> Current Z = 1.23\n', encoding="utf-8")
        folder = self.root / "Monitoring" / (self.started.strftime("%Y%m%d_%H%M%S") + "_1820-layer")
        folder.mkdir(parents=True)
        self.path = folder / ("LayerDatasheet_" + self.started.strftime("%Y%m%d_%H%M%S") + ".csv")
        self.header = list(MonitoringCsvReader.REQUIRED)
        self.old = self.row(1057, self.started - timedelta(seconds=60), self.started - timedelta(seconds=30), 41)
        self.anchor = self.row(1058, self.started, self.started + timedelta(seconds=30), 42)
        self.next = self.row(1059, self.started + timedelta(seconds=31), self.now, 43)
        self.write([self.old, self.anchor])
        self.reader = self.make_reader()

    def make_reader(self):
        return MonitoringCsvReader(self.target, roots=[self.root / "Monitoring"], clock=lambda: self.now.timestamp() + .5)

    def row(self, index, start, finish, temp):
        fmt = lambda t: t.strftime("%m/%d/%Y %H:%M:%S")
        return [str(index + 1), fmt(start), fmt(finish - timedelta(seconds=1)), fmt(finish), str(temp)]

    def write(self, rows):
        self.path.write_bytes(encode([self.header] + rows))

    def append(self, raw):
        with self.path.open("ab") as stream:
            stream.write(raw)

    def test_real_job_log_resume_baselines_then_accepts_fresh_row(self):
        baseline = self.reader.read()
        self.assertEqual(1058, baseline.context.first_layer)
        self.assertEqual(self.started, baseline.context.first_started)
        self.assertEqual((1058, 42, False), (baseline.sample.layer_index, baseline.sample.temperature, baseline.is_new))
        self.append(encode([self.next]))
        result = self.reader.read()
        self.assertEqual((1059, 43, True), (result.sample.layer_index, result.sample.temperature, result.is_new))
        self.assertIsNotNone(result.token)
        self.assertFalse(self.reader.read().is_new)

    def test_prefix_and_partial_anchor_never_produce_sample_or_token(self):
        self.write([self.old])
        first = self.reader.read()
        self.assertIsNone(first.sample)
        self.assertIsNone(first.token)
        raw = encode([self.anchor])
        self.append(raw[:-3])
        partial = self.reader.read()
        self.assertIsNone(partial.sample)
        self.assertIsNone(partial.token)
        self.append(raw[-3:])
        anchor = self.reader.read()
        self.assertEqual(1058, anchor.sample.layer_index)
        self.assertFalse(anchor.is_new)
        self.append(encode([self.next]))
        self.assertTrue(self.reader.read().is_new)

    def test_contradictory_bounds_and_missing_anchor_fail_closed(self):
        cases = [
            [self.row(1057, self.started, self.started + timedelta(seconds=10), 40)],
            [self.row(1058, self.started - timedelta(seconds=30), self.started - timedelta(seconds=10), 40)],
            [self.old, self.row(1059, self.started, self.started + timedelta(seconds=10), 40)],
            [self.old, self.row(1058, self.started + timedelta(seconds=3), self.started + timedelta(seconds=10), 40)],
            [self.old, self.anchor, self.row(1056, self.started - timedelta(seconds=80), self.started - timedelta(seconds=40), 40)],
            [self.row(1057, self.started - timedelta(seconds=20), self.started - timedelta(seconds=2), 40)],
            [self.row(1057, self.started - timedelta(seconds=20), self.started - timedelta(seconds=30), 40)],
        ]
        for rows in cases:
            with self.subTest(rows=rows):
                self.write(rows)
                with self.assertRaises(CsvSourceError):
                    self.make_reader().read()

    def test_anchor_tolerance_and_history_cutoff_boundaries(self):
        for offset in (-2, 2, -3, 3):
            with self.subTest(anchor_offset=offset):
                anchor = self.row(1058, self.started + timedelta(seconds=offset), self.started + timedelta(seconds=30), 42)
                self.write([self.old, anchor])
                if abs(offset) <= 2:
                    self.assertEqual(1058, self.make_reader().read().sample.layer_index)
                else:
                    with self.assertRaises(CsvSourceError):
                        self.make_reader().read()
        for offset in (-2, -1, 0):
            with self.subTest(history_finish_offset=offset):
                history = self.row(1057, self.started - timedelta(seconds=20), self.started + timedelta(seconds=offset), 41)
                self.write([history, self.anchor])
                with self.assertRaises(CsvSourceError):
                    self.make_reader().read()

    def test_appended_history_after_anchor_is_rejected(self):
        self.reader.read()
        self.append(encode([self.old]))
        with self.assertRaises(CsvSourceError):
            self.reader.read()

    def test_current_only_later_first_row_remains_supported(self):
        self.write([self.row(1060, self.started + timedelta(seconds=10), self.started + timedelta(seconds=30), 45)])
        result = self.make_reader().read()
        self.assertEqual(1060, result.sample.layer_index)
        self.assertFalse(result.is_new)

    def test_actual_adapter_reconnect_baseline_then_one_mocked_save(self):
        from test_windows_ui_adapter import AdapterTests
        with tempfile.TemporaryDirectory() as directory:
            adapter, state, calls, path, values, append, initial, source, audit = AdapterTests.fixture(self, directory)
            adapter.disconnect()
            log = Path(directory) / "Logs" / "20261027_M1.log"
            log.write_text(log.read_text(encoding="utf-8").replace("Layer: 0", "Layer: 1058"), encoding="utf-8")
            stamp = datetime.strptime(source.name[len("LayerDatasheet_"):-len(".csv")], "%Y%m%d_%H%M%S")
            old = self.row(1057, stamp - timedelta(seconds=60), stamp - timedelta(seconds=30), 41)
            anchor = self.row(1058, stamp, stamp + timedelta(seconds=30), 42)
            source.write_bytes(encode([self.header, old, anchor]))
            stop = threading.Event()
            target = MachineWindow("M1", 1234, 5678, "Own synthetic target", str(Path(directory) / "Operator.exe"))
            adapter.connect(target, stop)
            adapter.prepare_mode("control", stop)
            baseline = adapter.read_snapshot(stop)
            self.assertEqual(1058, baseline.layer_index)
            self.assertFalse(baseline.observation_is_new)
            with self.assertRaises(ControlUnavailable):
                adapter.apply_ir_power(1, 67, baseline.observation_key, stop)
            self.assertEqual(0, state["saves"])
            row = self.row(1059, stamp + timedelta(seconds=31), datetime.now().replace(microsecond=0), 43)
            with source.open("ab") as stream:
                stream.write(encode([row]))
            with log.open("a", encoding="utf-8") as stream:
                stream.write(datetime.now().strftime("%Y-%m-%d%p%I%M%S") + "...[BJT - AUTO]: Layer: 1060 -> Current Z = 1.1\n")
            adapter._last_read = 0
            fresh = adapter.read_snapshot(stop)
            self.assertTrue(fresh.observation_is_new)
            adapter.apply_ir_power(1, 67, fresh.observation_key, stop)
            with self.assertRaises(ControlUnavailable):
                adapter.apply_ir_power(1, 67, fresh.observation_key, stop)
            self.assertEqual(1, state["saves"])
            adapter.disconnect()

    def test_invalid_history_is_not_silently_skipped(self):
        for value in (-1, "NaN", "bad"):
            with self.subTest(value=value):
                bad = self.row(1057, self.started - timedelta(seconds=60), self.started - timedelta(seconds=30), value)
                self.write([bad, self.anchor])
                with self.assertRaises(CsvSourceError):
                    self.make_reader().read()

    def test_same_size_rewrite_rebaselines_and_revalidates_history(self):
        self.reader.read()
        stat = self.path.stat()
        original = self.path.read_bytes()
        rewritten = original.replace(b',42\r\n', b',43\r\n')
        self.assertNotEqual(original, rewritten)
        self.assertEqual(len(original), len(rewritten))
        self.path.write_bytes(rewritten)
        os.utime(self.path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        result = self.reader.read()
        self.assertEqual(43, result.sample.temperature)
        self.assertFalse(result.is_new)
        bad = rewritten.replace(b',41\r\n', b',-1\r\n')
        self.path.write_bytes(bad)
        os.utime(self.path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        with self.assertRaises(CsvSourceError):
            self.reader.read()

    def test_job_change_clears_unverified_prefix(self):
        self.write([self.old])
        self.assertIsNone(self.reader.read().sample)
        # A different job has a different anchor, even when it selects the same CSV.
        context = JobContext("different-job", 1059, self.started, 1059, "AUTO")
        self.reader.log = Mock(read=Mock(return_value=context))
        self.write([self.row(1059, self.started, self.started + timedelta(seconds=30), 44)])
        self.reader._last_scan = 0
        result = self.reader.read()
        self.assertEqual(1059, result.sample.layer_index)
        self.assertFalse(result.is_new)

    def test_session_reconnect_never_calculates_or_saves_history_or_baseline(self):
        # The adapter is fake; its observations come from the real CSV reader.
        self.write([self.old])
        adapter = Mock()
        stop = threading.Event()
        reports = []
        calls = []
        def read(_stop):
            count = len(calls)
            if count == 1:
                self.append(encode([self.anchor]))
            elif count == 2:
                self.append(encode([self.next]))
            result = self.reader.read()
            calls.append(result)
            sample = result.sample
            return UiSnapshot(self.target, layer_index=sample.layer_index if sample else None,
                t_recoating_c=sample.temperature if sample else None,
                observation_key=sample.key if sample and result.token else None,
                observation_is_new=result.is_new, run_key=result.context.run_key,
                heater1_setting_percent=50, heater2_setting_percent=50,
                coupled_output=True, control_available=True, machine_state="AUTO", minimum_poll_seconds=0)
        adapter.read_snapshot.side_effect = read
        controller = Mock()
        controller.process.return_value = type("Result", (), {"power_percent": 55, "reason": "test"})()
        def emit(kind, payload):
            if kind == "snapshot":
                reports.append(payload)
                if len(reports) < 3:
                    controller.process.assert_not_called()
                    adapter.apply_ir_power.assert_not_called()
                else:
                    stop.set()
        with patch("session.LayerAdaptiveController", return_value=controller):
            run_ui_session(SessionOptions(self.target, mode="control"), stop, emit, lambda: adapter)
        controller.process.assert_called_once()
        self.assertEqual(1060, controller.process.call_args.args[0])
        adapter.apply_ir_power.assert_called_once()
        self.assertEqual([False, False, True], [item.is_new for item in calls])


if __name__ == "__main__":
    unittest.main()
