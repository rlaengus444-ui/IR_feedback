from pathlib import Path
from dataclasses import replace
import tempfile
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

from output_limit import MaximumIrPreference
from session import SessionController, WorkerEvent
from standalone_ir import ThermalFeedbackWindow
from test_session import TARGET, DeferredThread, snapshot


class PreferenceTests(unittest.TestCase):
    def test_missing_default_atomic_roundtrip_and_invalid_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'preferences/limit.json'
            preference = MaximumIrPreference(path)
            self.assertEqual(100, preference.load())
            preference.save(62.5)
            self.assertEqual(62.5, preference.load())
            with patch('output_limit.os.replace', side_effect=OSError('blocked')):
                with self.assertRaises(OSError):
                    preference.save(40)
            self.assertEqual(62.5, preference.load())
            self.assertEqual([path], list(path.parent.iterdir()))
            for invalid in ('{}', 'not json', '{"version":true,"maximum_ir_percent":60}',
                            '{"version":1,"maximum_ir_percent":101}',
                            '{"version":1,"maximum_ir_percent":NaN}'):
                path.write_text(invalid)
                with self.assertRaises(ValueError):
                    preference.load()


class MaximumIrUITests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(str(error))
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.preference = MaximumIrPreference(Path(self.temporary.name) / 'limit.json')
        self.controller = SessionController(Mock(), thread_factory=DeferredThread)
        self.view = ThermalFeedbackWindow(self.root, controller=self.controller, auto_discover=False,
                                          maximum_ir_preference=self.preference)
        self.addCleanup(self.view.archive.close)
        self.view.targets = [TARGET]
        self.view._populate_hosts()

    def connected(self, minimum=0):
        self.controller.maximum_ir_setting.bind_bounds(minimum, 100)
        self.controller.maximum_ir_setting.activate(self.controller.maximum_ir_setting.snapshot())
        self.controller.events.put(WorkerEvent(self.controller.generation, 'connected', {}))
        self.view._pump()

    def test_cap_remains_editable_and_apply_only_requests_next_layer(self):
        self.view.maximum_ir_percent.set('62.5')
        with patch.object(self.controller, 'connect', wraps=self.controller.connect) as connect:
            self.view.connect()
        self.assertEqual(62.5, self.preference.load())
        self.assertEqual(62.5, connect.call_args.args[0].maximum_ir_percent)
        self.assertEqual('normal', str(self.view.maximum_ir_entry['state']))
        self.assertEqual('disabled', str(self.view.maximum_ir_apply_button['state']))
        self.connected()
        self.assertEqual('normal', str(self.view.maximum_ir_apply_button['state']))
        generation = self.controller.generation
        self.view.maximum_ir_percent.set('55')
        self.view.apply_maximum_ir()
        self.assertEqual(55, self.preference.load())
        self.assertEqual((1, 55.0), self.controller.maximum_ir_setting.snapshot())
        self.assertIn('요청 55%', self.view.maximum_ir_status.get())
        self.assertIn('현재 적용 상한 62.5%', self.view.maximum_ir_status.get())
        self.assertEqual(generation, self.controller.generation)
        self.assertFalse(self.controller.stop_event.is_set())
        self.controller.runner.assert_not_called()
        self.view.disconnect()
        self.assertEqual('disabled', str(self.view.maximum_ir_entry['state']))
        self.controller.events.put(WorkerEvent(self.controller.generation, 'finished', None))
        self.view._pump()
        self.assertEqual('normal', str(self.view.maximum_ir_entry['state']))
        self.assertEqual('55', self.view.maximum_ir_percent.get())

    def test_minimum_rejection_preserves_active_cap_and_saved_preference(self):
        self.view.maximum_ir_percent.set('62.5')
        self.view.connect()
        self.connected(minimum=50)
        stamp = self.controller.maximum_ir_setting.snapshot()
        self.view.maximum_ir_percent.set('40')
        self.view.apply_maximum_ir()
        self.assertEqual(stamp, self.controller.maximum_ir_setting.snapshot())
        self.assertEqual(62.5, self.preference.load())
        self.assertIn('62.5%', self.view.maximum_ir_status.get())
        self.assertIn('하한', self.view.log.get('1.0', 'end'))

    def test_stale_ui_ack_keeps_pending_request_and_confirmed_save_marker(self):
        self.view.maximum_ir_percent.set('80')
        self.view.connect()
        self.connected()
        old_stamp = self.controller.maximum_ir_setting.snapshot()
        old_status = self.controller.maximum_ir_setting.status()
        self.view.maximum_ir_percent.set('60')
        self.view.apply_maximum_ir()
        data = dict(snapshot=replace(snapshot('saved', 1), heater1_setting_percent=80,
                    heater2_setting_percent=80), effective_mode='control',
                    proposed={1: 75}, reasons={1: 'old decision'}, applied={1: 75},
                    status='old', decision_cap_stamp=old_stamp, limit_status=old_status)
        self.controller.events.put(WorkerEvent(self.controller.generation, 'snapshot', data))
        self.view._pump()
        self.assertEqual('60', self.view.maximum_ir_percent.get())
        self.assertIn('요청 60%', self.view.maximum_ir_status.get())
        self.assertEqual(75, self.view.charts.history.points[0].ir1_saved)
        self.assertEqual('—', self.view.outputs.item('heater1')['values'][1])

    def test_close_saves_last_requested_limit_not_an_unapplied_draft(self):
        self.view.maximum_ir_percent.set('80')
        self.view.connect()
        self.connected()
        self.view.maximum_ir_percent.set('60')
        self.view.apply_maximum_ir()
        self.view.maximum_ir_percent.set('40')
        self.view.close()
        self.assertEqual(60, self.preference.load())

    def test_storage_failure_does_not_rollback_accepted_cap_request(self):
        self.view.maximum_ir_percent.set('80')
        self.view.connect()
        self.connected()
        self.view.maximum_ir_percent.set('60')
        with patch.object(self.preference, 'save', side_effect=OSError('blocked')), \
                patch('standalone_ir.write_error_log'):
            self.view.apply_maximum_ir()
        self.assertEqual((1, 60.0), self.controller.maximum_ir_setting.snapshot())
        self.assertFalse(self.controller.stop_event.is_set())
        self.assertIn('요청은 접수됨', self.view.log.get('1.0', 'end'))

    def test_invalid_cap_never_starts_a_worker_and_wait_mode_keeps_pumping(self):
        self.view.maximum_ir_percent.set('101')
        self.view.connect()
        self.assertIsNone(self.controller.thread)
        self.assertIn('최대 IR', self.view.log.get('1.0', 'end'))
        self.view.maximum_ir_percent.set('60')
        self.view.connect()
        self.controller.events.put(WorkerEvent(self.controller.generation, 'connected', {}))
        data = dict(snapshot=snapshot('new', 4), effective_mode='power-limit-waiting',
                    proposed={}, reasons={}, applied={}, status='최대 IR 초과 대기')
        self.controller.events.put(WorkerEvent(self.controller.generation, 'snapshot', data))
        with patch.object(self.root, 'after', wraps=self.root.after) as schedule:
            self.view._pump()
        self.assertEqual('connected', self.controller.state)
        self.assertIn('최대 IR', self.view.status.get())
        self.assertTrue(any(c.args == (50, self.view._pump) for c in schedule.call_args_list))

    def test_corrupt_preference_requires_explicit_input_and_close_stores_valid_cap(self):
        self.preference.path.write_text('{"version":1,"maximum_ir_percent":500}')
        other = tk.Toplevel(self.root)
        view = ThermalFeedbackWindow(other, controller=SessionController(thread_factory=DeferredThread),
            auto_discover=False, maximum_ir_preference=self.preference)
        self.addCleanup(view.archive.close)
        self.assertEqual('', view.maximum_ir_percent.get())
        self.assertIn('다시 입력', view.capability.get())
        view.maximum_ir_percent.set('45.5')
        view.close()
        self.assertEqual(45.5, self.preference.load())


if __name__ == '__main__':
    unittest.main()
