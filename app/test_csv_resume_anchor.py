"""Synthetic incomplete resume-anchor regression tests; no equipment or Excel."""
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from adapter_contracts import ControlUnavailable, MachineWindow, UiSnapshot
from csv_source import CsvSourceError, JobContext
from session import SessionOptions, run_ui_session
import test_csv_reconnect as reconnect
from test_csv_reconnect import encode


class ResumeAnchorTests(unittest.TestCase):
    setUp = reconnect.ReconnectTests.setUp
    make_reader = reconnect.ReconnectTests.make_reader
    row = reconnect.ReconnectTests.row
    write = reconnect.ReconnectTests.write
    append = reconnect.ReconnectTests.append

    def metadata(self, finish='', temp='0', start=None):
        row = list(self.anchor)
        row[1] = self.anchor[1] if start is None else start
        row[3] = finish
        row[4] = str(temp)
        return row

    def assert_no_observation(self, result):
        self.assertIsNone(result.sample)
        self.assertIsNone(result.token)
        self.assertFalse(result.is_new)

    def test_incomplete_metadata_formats_anchor_and_later_first_sample_is_baseline(self):
        formats = [('', '0'), ('01/01/0001 00:00:00', '0'),
                   ('0001-01-01 00:00:00', '42'), (self.anchor[3], '0')]
        for finish, temp in formats:
            with self.subTest(finish=finish, temp=temp):
                self.write([self.old, self.metadata(finish, temp)])
                reader = self.make_reader()
                self.assert_no_observation(reader.read())
                self.append(encode([self.next]))
                baseline = reader.read()
                self.assertEqual(1059, baseline.sample.layer_index)
                self.assertFalse(baseline.is_new)
                self.assertIsNotNone(baseline.token)
                fresh = self.row(1060, self.started + timedelta(seconds=32), self.now, 44)
                self.append(encode([fresh]))
                result = reader.read()
                self.assertEqual((1060, True), (result.sample.layer_index, result.is_new))

    def test_incomplete_anchor_and_sample_in_same_batch_baseline(self):
        self.write([self.old, self.metadata(), self.next])
        baseline = self.reader.read()
        self.assertEqual(1059, baseline.sample.layer_index)
        self.assertFalse(baseline.is_new)
        self.append(encode([self.row(1060, self.started + timedelta(seconds=32), self.now, 44)]))
        self.assertTrue(self.reader.read().is_new)

    def test_malformed_or_contradictory_anchor_is_rejected(self):
        cases = [
            [self.old, self.metadata('nonsense')],
            [self.old, self.metadata('', -1)],
            [self.old, self.metadata(self.anchor[3], -1)],
            [self.old, self.metadata('', 0, '')],
            [self.old, self.metadata('', 0, '01/01/0001 00:00:00')],
            [self.old, self.metadata('', 0, (self.started + timedelta(seconds=3)).strftime('%m/%d/%Y %H:%M:%S'))],
            [self.old, self.next],
            [self.old, self.metadata('broken', 0), self.next],
            [self.old, self.metadata(self.anchor[3], 0), self.metadata((self.started + timedelta(seconds=31)).strftime('%m/%d/%Y %H:%M:%S'), 0)],
            [self.old, self.metadata(), self.metadata('', 0, (self.started + timedelta(seconds=1)).strftime('%m/%d/%Y %H:%M:%S'))],
            [self.old, self.metadata(), self.old],
            [self.old, self.metadata((self.started - timedelta(seconds=1)).strftime('%m/%d/%Y %H:%M:%S'))],
            [self.old, self.metadata('', 'NaN')],
            [self.old, self.metadata('', 'bad')],
            [self.old, self.metadata()[:-1]],
        ]
        for rows in cases:
            with self.subTest(rows=rows):
                self.write(rows)
                with self.assertRaises(CsvSourceError):
                    self.make_reader().read()

    def test_partial_tail_does_not_consume_first_baseline(self):
        self.write([self.old, self.metadata()])
        self.assert_no_observation(self.reader.read())
        tail = encode([self.metadata()])
        self.append(encode([self.next]) + tail[:-3])
        partial = self.reader.read()
        self.assertFalse(partial.is_new)
        self.assertIsNone(partial.token)
        self.append(tail[-3:])
        complete = self.reader.read()
        self.assertEqual(1059, complete.sample.layer_index)
        self.assertFalse(complete.is_new)
        self.assertIsNotNone(complete.token)
        self.append(encode([self.row(1060, self.started + timedelta(seconds=32), self.now, 44)]))
        self.assertTrue(self.reader.read().is_new)

    def test_chunked_append_first_sample_is_still_baseline_at_full_token(self):
        self.write([self.old, self.metadata()])
        self.assert_no_observation(self.reader.read())
        future = ['2000', '01/01/0001 00:00:00', '01/01/0001 00:00:00', '01/01/0001 00:00:00', '0']
        blob = encode([self.next] + [future] * 20000)
        self.assertGreater(len(blob), 1024 * 1024)
        self.append(blob)
        partial = self.reader.read()
        self.assertFalse(partial.is_new)
        self.assertIsNone(partial.token)
        full = self.reader.read()
        self.assertIsNotNone(full.token)
        self.assertFalse(full.is_new)
        self.assertEqual(1059, full.sample.layer_index)
        self.append(encode([self.row(1060, self.started + timedelta(seconds=32), self.now, 44)]))
        self.assertTrue(self.reader.read().is_new)

    def test_rewrite_and_job_change_do_not_carry_anchor(self):
        for change in ('rewrite', 'new_job'):
            with self.subTest(change=change):
                self.write([self.old, self.metadata()])
                reader = self.make_reader()
                self.assert_no_observation(reader.read())
                self.write([self.old, self.next])
                if change == 'new_job':
                    reader.log = Mock(read=Mock(return_value=JobContext('other', 1058, self.started, 1059, 'AUTO')))
                    reader._last_scan = 0
                with self.assertRaises(CsvSourceError):
                    reader.read()

    def test_current_only_later_row_remains_compatible(self):
        self.write([self.next])
        result = self.reader.read()
        self.assertEqual(1059, result.sample.layer_index)
        self.assertFalse(result.is_new)

    def test_mock_session_uses_only_new_row_for_calculation_and_save(self):
        self.write([self.old, self.metadata()])
        adapter = Mock()
        stop = threading.Event()
        observed = []
        def read(_stop):
            index = len(observed)
            if index == 1:
                self.append(encode([self.next]))
            if index == 2:
                self.append(encode([self.row(1060, self.started + timedelta(seconds=32), self.now, 44)]))
            result = self.reader.read()
            observed.append(result)
            sample = result.sample
            return UiSnapshot(self.target, layer_index=sample.layer_index if sample else None,
                t_recoating_c=sample.temperature if sample else None,
                observation_key=sample.key if sample and result.token else None,
                observation_is_new=result.is_new, run_key=result.context.run_key,
                heater1_setting_percent=50, heater2_setting_percent=50, coupled_output=True,
                control_available=True, machine_state='AUTO', minimum_poll_seconds=0)
        adapter.read_snapshot.side_effect = read
        controller = Mock()
        controller.process.return_value = type('Result', (), {'power_percent': 55, 'reason': 'test'})()
        reports = []
        def emit(kind, payload):
            if kind == 'snapshot':
                reports.append(payload)
                if len(reports) < 3:
                    controller.process.assert_not_called()
                    adapter.apply_ir_power.assert_not_called()
                else:
                    stop.set()
        with patch('session.LayerAdaptiveController', return_value=controller):
            run_ui_session(SessionOptions(self.target, mode='control'), stop, emit, lambda: adapter)
        self.assertEqual([False, False, True], [result.is_new for result in observed])
        controller.process.assert_called_once()
        adapter.apply_ir_power.assert_called_once()

    def test_real_adapter_mock_transport_incomplete_anchor_baseline_and_one_save(self):
        from test_windows_ui_adapter import AdapterTests
        with tempfile.TemporaryDirectory() as directory:
            adapter, state, calls, path, values, append, initial, source, audit = AdapterTests.fixture(self, directory)
            adapter.disconnect()
            log = Path(directory) / 'Logs' / '20261027_M1.log'
            log.write_text(log.read_text(encoding='utf-8').replace('Layer: 0', 'Layer: 1058'), encoding='utf-8')
            stamp = datetime.strptime(source.name[len('LayerDatasheet_'):-len('.csv')], '%Y%m%d_%H%M%S')
            old = self.row(1057, stamp - timedelta(seconds=60), stamp - timedelta(seconds=30), 41)
            metadata = self.row(1058, stamp, stamp + timedelta(seconds=30), 0)
            metadata[3] = ''
            source.write_bytes(encode([self.header, old, metadata]))
            stop = threading.Event()
            target = MachineWindow('M1', 1234, 5678, 'fixture', str(Path(directory) / 'Operator.exe'))
            adapter.connect(target, stop)
            adapter.prepare_mode('control', stop)
            first = adapter.read_snapshot(stop)
            self.assertIsNone(first.observation_key)
            self.assertEqual(0, state['saves'])
            baseline_row = self.row(1059, stamp + timedelta(seconds=31), datetime.now().replace(microsecond=0), 43)
            with source.open('ab') as stream:
                stream.write(encode([baseline_row]))
            adapter._last_read = 0
            baseline = adapter.read_snapshot(stop)
            self.assertFalse(baseline.observation_is_new)
            with self.assertRaises(ControlUnavailable):
                adapter.apply_ir_power(1, 67, baseline.observation_key, stop)
            self.assertEqual(0, state['saves'])
            fresh_row = self.row(1060, stamp + timedelta(seconds=32), datetime.now().replace(microsecond=0), 44)
            with source.open('ab') as stream:
                stream.write(encode([fresh_row]))
            with log.open('a', encoding='utf-8') as stream:
                stream.write(datetime.now().strftime('%Y-%m-%d%p%I%M%S') + '...[BJT - AUTO]: Layer: 1061 -> Current Z = 1.1\n')
            adapter._last_read = 0
            fresh = adapter.read_snapshot(stop)
            self.assertTrue(fresh.observation_is_new)
            adapter.apply_ir_power(1, 67, fresh.observation_key, stop)
            with self.assertRaises(ControlUnavailable):
                adapter.apply_ir_power(1, 67, fresh.observation_key, stop)
            self.assertEqual(1, state['saves'])
            adapter.disconnect()


if __name__ == '__main__':
    unittest.main()
