"""Pre-edit CSV races stay connected; no old command may be replayed."""
from dataclasses import replace
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from adapter_contracts import (AdapterError, AmbiguousWriteError, ControlUnavailable,
                              ObservationSuspended, UiAdapter)
from csv_source import CsvSourceError
from session import SessionOptions, run_ui_session
from test_session import TARGET, snapshot
import test_windows_ui_adapter as fixtures
from windows_ui_adapter import WindowsUiAdapter


class ObservationAdapterTests(unittest.TestCase):
    def test_newer_complete_row_cancels_old_decision_and_preserves_new_key(self):
        with tempfile.TemporaryDirectory() as d:
            a, state, calls, _, _, append, _, _, _ = fixtures.AdapterTests.fixture(self, d)
            observed = append()
            state['hook'] = lambda request: append(index=2, temperature=44)
            with self.assertRaises(ObservationSuspended):
                a.apply_ir_power(1, 67, observed.observation_key, threading.Event())
            self.assertEqual(0, state['saves'])
            self.assertFalse(any(c['operation'] == 'apply' for c in calls))
            self.assertIsNone(a._control_fault)
            self.assertIsNone(a._control_hold_reason)
            self.assertIsNone(a._last_snapshot)
            a._last_read = 0
            fresh = a.read_snapshot(threading.Event())
            self.assertEqual(2, fresh.layer_index)
            self.assertTrue(fresh.observation_is_new)
            self.assertTrue(fresh.control_available)

    def test_partial_append_waits_then_receives_completed_row(self):
        with tempfile.TemporaryDirectory() as d:
            a, state, calls, _, _, append, _, source, _ = fixtures.AdapterTests.fixture(self, d)
            observed = append()
            state['hook'] = lambda request: append(index=2, temperature=44, partial=True)
            with self.assertRaises(ObservationSuspended):
                a.apply_ir_power(1, 67, observed.observation_key, threading.Event())
            self.assertEqual(0, state['saves'])
            self.assertFalse(any(c['operation'] == 'apply' for c in calls))
            self.assertIsNone(a._control_fault)
            with source.open('ab') as stream:
                stream.write(b'4\r\n')
            a._last_read = 0
            fresh = a.read_snapshot(threading.Event())
            self.assertEqual((2, 44), (fresh.layer_index, fresh.t_recoating_c))
            self.assertTrue(fresh.observation_is_new)

    def test_known_native_wait_only_recovers_before_markers(self):
        for marker in (None, 'edit_marker', 'save_marker'):
            with self.subTest(marker=marker), tempfile.TemporaryDirectory() as d:
                a, state, _, _, _, append, _, _, _ = fixtures.AdapterTests.fixture(self, d)
                observed = append()
                original = a._runner
                def runner(request, stop):
                    if request['operation'] == 'apply':
                        if marker:
                            Path(request[marker]).write_text('started')
                        return {'success': False, 'error_kind': 'observation_wait',
                                'error': 'CSV changed after the decision'}
                    return original(request, stop)
                a._runner = runner
                with self.assertRaises(AmbiguousWriteError if marker else ObservationSuspended):
                    a.apply_ir_power(1, 67, observed.observation_key, threading.Event())
                self.assertEqual(0, state['saves'])
                self.assertIsNone(a._control_hold_reason)
                if marker:
                    self.assertIsNotNone(a._control_fault)
                else:
                    self.assertIsNone(a._control_fault)
                    a._last_read = 0
                    self.assertTrue(a.read_snapshot(threading.Event()).control_available)

    def test_native_preflight_wait_does_not_enter_apply(self):
        with tempfile.TemporaryDirectory() as d:
            a, state, calls, _, _, append, _, _, _ = fixtures.AdapterTests.fixture(self, d)
            observed = append()
            original = a._runner
            def runner(request, stop):
                if request['operation'] == 'verify_control':
                    return {'success': False, 'error_kind': 'observation_wait', 'error': 'CSV grew'}
                return original(request, stop)
            a._runner = runner
            with self.assertRaises(ObservationSuspended):
                a.apply_ir_power(1, 67, observed.observation_key, threading.Event())
            self.assertIsNone(a._control_fault)
            self.assertEqual(0, state['saves'])
            self.assertFalse(any(c['operation'] == 'apply' for c in calls))

    def test_job_changes_and_parser_errors_remain_terminal(self):
        with tempfile.TemporaryDirectory() as d:
            a, _, _, _, _, append, _, _, _ = fixtures.AdapterTests.fixture(self, d)
            observed = append()
            original = a._csv.read()
            changed = replace(original, context=replace(original.context, run_key='different-job'))
            with patch.object(a._csv, 'read', return_value=changed):
                with self.assertRaises(ControlUnavailable) as error:
                    a._verify_csv_observation(observed.observation_key)
                self.assertNotIsInstance(error.exception, ObservationSuspended)
            for error in (CsvSourceError('invalid -1 reading'), AdapterError('unknown read failure')):
                with patch.object(a._csv, 'read', side_effect=error):
                    with self.assertRaises(type(error)):
                        a._verify_csv_observation(observed.observation_key)


class ObservationSessionTests(unittest.TestCase):
    def run_frames(self, frames, *, errors, coupled=True, algorithm='basic'):
        adapter = Mock(spec=UiAdapter)
        adapter.read_snapshot.side_effect = frames
        adapter.apply_ir_power.side_effect = errors
        stop, reports = threading.Event(), []
        controller = Mock()
        controller.process.return_value = type('Result', (), {'power_percent':55, 'reason':'test'})()
        def emit(kind, value):
            if kind == 'snapshot':
                reports.append(value)
                if len(reports) == len(frames):
                    stop.set()
        options = SessionOptions(TARGET, mode='control')
        if algorithm == 'excel':
            from test_slice_handoff import SPEC, A
            options = SessionOptions(TARGET, mode='control', workbook_spec=SPEC, slice_dataset=A)
        with patch('session.LayerAdaptiveController', return_value=controller), patch('session.WorkbookController', return_value=controller):
            run_ui_session(options, stop, emit, lambda:adapter)
        return adapter, controller, reports

    def frame(self, key, layer, **kw):
        return replace(snapshot(key, layer, control_available=True, observation_is_new=True), **kw)

    def test_baseline_cached_same_layer_and_fresh_layer_before_resume(self):
        for algorithm in ('basic', 'excel'):
            with self.subTest(algorithm=algorithm):
                frames = [self.frame('initial', 254, observation_is_new=False),
                          self.frame('failed', 255), self.frame('failed', 255),
                          self.frame('cached', 256, observation_is_new=False),
                          self.frame('same-layer', 256), self.frame('fresh', 257)]
                adapter, controller, reports = self.run_frames(frames,
                    errors=[ObservationSuspended('CSV recording'), None], algorithm=algorithm)
                self.assertEqual(['failed', 'fresh'], [c.args[2] for c in adapter.apply_ir_power.call_args_list])
                self.assertEqual([{}, {}, {}, {}, {}, {1:55}], [r['applied'] for r in reports])
                self.assertTrue(all(not r['proposed'] for r in reports[1:-1]))
                self.assertIn('CSV 갱신 대기', reports[1]['status'])
                self.assertEqual(2 if algorithm == 'basic' else 1, controller.reset.call_count)
                self.assertEqual([256, 258], [c.args[0] for c in controller.process.call_args_list])
                adapter.suspend_control.assert_not_called()
                adapter.disconnect.assert_called_once()  # Normal end after final frame.

    def test_no_new_layer_keeps_observing_without_retry(self):
        frames = [self.frame('old', 254, observation_is_new=False), self.frame('failed', 255)]
        frames += [self.frame('failed', 255)] * 5
        adapter, controller, reports = self.run_frames(frames, errors=[ObservationSuspended('partial tail')])
        self.assertEqual(7, len(reports))
        self.assertEqual(1, adapter.apply_ir_power.call_count)
        self.assertFalse(any(r['applied'] for r in reports))
        self.assertFalse(any(r['proposed'] for r in reports[1:]))

    def test_other_terminal_errors_are_not_swallowed(self):
        for error in (ControlUnavailable('wrong job'), CsvSourceError('invalid sensor'),
                      AmbiguousWriteError('edit may have started'), AdapterError('unknown failure')):
            with self.subTest(error=error):
                frames = [self.frame('old', 254, observation_is_new=False), self.frame('failed', 255)]
                with self.assertRaises(type(error)):
                    self.run_frames(frames, errors=[error])

    def test_second_channel_success_record_survives_later_preedit_wait(self):
        frames = [self.frame('old', 254, observation_is_new=False, coupled_output=False),
                  self.frame('failed', 255, coupled_output=False)]
        adapter, _, reports = self.run_frames(frames, errors=[None, ObservationSuspended('CSV changed')])
        self.assertEqual({1:55}, reports[-1]['applied'])
        self.assertEqual({}, reports[-1]['proposed'])
        self.assertEqual(2, adapter.apply_ir_power.call_count)


if __name__ == '__main__':
    unittest.main()
