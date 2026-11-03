"""Threaded session/Excel-owner integration tests; all external I/O is fake."""
import threading
from dataclasses import replace
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from adapter_contracts import AmbiguousWriteError, MachineWindow, UiAdapter, UiSnapshot
from excel_async import AsyncWorkbookGroup, ExcelGroupFence
from excel_workbook import WorkbookSpec, WorkbookTimeout
from session import SessionController, SessionOptions, run_ui_session
from slice_data import SliceConfig, SliceDataset, SliceRecord

TARGET = MachineWindow('M1', 1, 2, 'fake', 'fake.exe')
SPEC = WorkbookSpec('fake.xlsx', 'Sheet1', 'A13', 'B13', 'C13', 'D1')


def make_dataset(name, coverage):
    return SliceDataset(SliceConfig(2), tuple(
        SliceRecord(layer, f'{layer}.png', coverage, coverage, coverage, 1, 1, 1, name)
        for layer in (1, 2, 3, 4)), name)


class Harness:
    def __init__(self, *, coupled=True, timeout=False, ambiguous=False, block_init=False, block_reset=False, image=False):
        self.init_entered, self.init_release = threading.Event(), threading.Event()
        self.reset_entered, self.reset_release = threading.Event(), threading.Event()
        self.block_init, self.block_reset = block_init, block_reset
        self.condition = threading.Condition()
        self.frame = self.row('baseline', 0, coupled)
        self.events = []
        self.calls = []
        self.reads = 0
        self.entered = threading.Event()
        self.release = threading.Event()
        self.timeout = timeout
        self.ambiguous = ambiguous
        self.group = None
        self.fence = ExcelGroupFence()
        self.error = None
        self.stop = threading.Event()
        self.adapter = Mock(spec=UiAdapter)
        self.adapter.read_snapshot.side_effect = self.read
        self.adapter.apply_ir_power.side_effect = self.apply
        spec = replace(SPEC, slice_start_cell='E13') if image else SPEC
        dataset = make_dataset('initial', 10) if image else None
        self.options = SessionOptions(TARGET, mode='control', workbook_spec=spec, slice_dataset=dataset)
        harness = self

        class Controller:
            def __init__(self, spec, target, **kwargs):
                self.closed = False
                harness.init_entered.set()
                if harness.block_init and not harness.init_release.wait(4):
                    raise RuntimeError("test did not release init")
            def set_target_temperature(self, target):
                pass
            def reset(self):
                harness.reset_entered.set()
                if harness.block_reset and not harness.reset_release.wait(4):
                    raise RuntimeError("test did not release reset")
            def process(self, layer, temperature, power, *, slice_features=None):
                with harness.condition:
                    harness.calls.append((layer, temperature, power, slice_features))
                    harness.condition.notify_all()
                harness.entered.set()
                if not harness.release.wait(4):
                    raise RuntimeError('test did not release controller')
                if harness.timeout:
                    raise WorkbookTimeout('fake timeout', operation='process', elapsed=1,
                                          deadline=1, phase='calculate', process_state='unknown')
                return SimpleNamespace(power_percent=power + 1, reason='fake')
            def close(self, force=False):
                self.closed = True
            def retirement_status(self):
                return {"verified": self.closed}
        self.factory = Controller

    @staticmethod
    def row(key, layer, coupled=True):
        return UiSnapshot(TARGET, layer_index=layer, t_recoating_c=80,
                          observation_key=key, observation_is_new=True,
                          heater1_setting_percent=50, heater2_setting_percent=50,
                          coupled_output=coupled, control_available=True,
                          minimum_poll_seconds=.1, machine_state='AUTO')

    def read(self, stop):
        with self.condition:
            self.reads += 1
            self.condition.notify_all()
            return self.frame

    def apply(self, heater, power, key, stop):
        if self.ambiguous and heater == 2:
            raise AmbiguousWriteError('heater 2 Save uncertain')

    def emit(self, kind, payload):
        with self.condition:
            self.events.append((kind, payload))
            self.condition.notify_all()

    def group_factory(self, *args, **kwargs):
        self.group = AsyncWorkbookGroup(*args, fence=self.fence, **kwargs)
        return self.group

    def start(self, wait_ready=True):
        def work():
            try:
                run_ui_session(self.options, self.stop, self.emit, lambda: self.adapter,
                               workbook_group_factory=self.group_factory)
            except Exception as error:
                self.error = error
            finally:
                self.emit('finished', None)
        self.patcher = patch('session.WorkbookController', self.factory)
        self.patcher.start()
        self.thread = threading.Thread(target=work, daemon=True)
        self.thread.start()
        if not wait_ready:
            return self
        self.wait_ready()
        return self

    def wait_ready(self):
        self.wait(lambda: self.group is not None and self.group.state == 'ready')
        # Ensure the ready event was consumed and the current row became baseline.
        self.wait(lambda: sum(k == 'snapshot' for k, _ in self.events) >= 2)
        return self

    def wait(self, predicate, seconds=4):
        deadline = time.monotonic() + seconds
        with self.condition:
            while not predicate():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError('timed out waiting for test condition')
                self.condition.wait(min(.05, remaining))

    def fresh(self, key='fresh', layer=1):
        with self.condition:
            self.frame = self.row(key, layer, self.frame.coupled_output)
            self.condition.notify_all()
        self.wait(self.entered.is_set)

    def close(self):
        self.stop.set()
        self.release.set()
        self.init_release.set()
        self.reset_release.set()
        self.thread.join(5)
        self.patcher.stop()
        if self.thread.is_alive():
            raise AssertionError('session thread did not stop')


class SessionAsyncTests(unittest.TestCase):
    def harness(self, **kwargs):
        h = Harness(**kwargs)
        self.addCleanup(h.close)
        return h.start()

    def test_blocked_excel_does_not_block_csv_or_save(self):
        h = self.harness()
        h.fresh()
        with h.condition:
            reads = h.reads
            snapshots = sum(k == 'snapshot' for k, _ in h.events)
        h.wait(lambda: h.reads >= reads + 3 and
               sum(k == 'snapshot' for k, _ in h.events) >= snapshots + 3)
        h.adapter.apply_ir_power.assert_not_called()
        self.assertEqual([(2, 80, 50, None)], h.calls)
        h.release.set()
        h.wait(lambda: h.adapter.apply_ir_power.call_count == 1)
        self.assertEqual((1, 51, 'fresh'), h.adapter.apply_ir_power.call_args.args[:3])
        self.assertIsNone(h.error)

    def test_typed_timeout_holds_session_until_disconnect(self):
        h = self.harness(timeout=True)
        h.fresh()
        h.release.set()
        h.wait(lambda: any(k == 'calculation_hold' for k, _ in h.events))
        with h.condition:
            reads = h.reads
        h.wait(lambda: h.reads >= reads + 3 and any(k == 'snapshot' and
               p['effective_mode'] == 'calculation-hold' for k, p in h.events))
        self.assertFalse(any(k in ('error', 'finished') for k, _ in h.events))
        self.assertTrue(h.thread.is_alive())
        h.adapter.apply_ir_power.assert_not_called()
        h.stop.set()
        h.thread.join(3)
        self.assertFalse(h.thread.is_alive())
        h.adapter.disconnect.assert_called_once()

    def test_watchdog_discards_late_result(self):
        h = self.harness()
        h.fresh()
        with h.group._condition:
            h.group._deadline = time.monotonic() - .01
        h.wait(lambda: any(k == 'calculation_hold' for k, _ in h.events))
        h.release.set()
        with h.condition:
            reads = h.reads
        h.wait(lambda: h.reads >= reads + 3)
        h.adapter.apply_ir_power.assert_not_called()
        self.assertTrue(h.thread.is_alive())

    def test_target_aba_during_process_invalidates_result_and_requires_fresh_row(self):
        h = self.harness()
        h.fresh()
        h.options._target_temperature_setting.set(70)
        h.wait(lambda: h.group.state == 'resetting')
        h.options._target_temperature_setting.set(65)
        h.release.set()
        h.wait(lambda: h.group.state == 'ready')
        with h.condition:
            reads = h.reads
        h.wait(lambda: h.reads >= reads + 3)
        h.adapter.apply_ir_power.assert_not_called()
        self.assertEqual(1, len(h.calls))
        h.fresh('later', 2)
        h.wait(lambda: h.adapter.apply_ir_power.call_count == 1)
        self.assertEqual([2, 3], [call[0] for call in h.calls])
        self.assertEqual('later', h.adapter.apply_ir_power.call_args.args[2])

    def test_two_heaters_finish_before_save_and_report_partial_ambiguous_save(self):
        h = self.harness(coupled=False, ambiguous=True)
        h.fresh()
        h.adapter.apply_ir_power.assert_not_called()
        h.release.set()
        h.wait(lambda: any(k == 'finished' for k, _ in h.events))
        self.assertEqual(2, len(h.calls))
        self.assertEqual([1, 2], [call.args[0] for call in h.adapter.apply_ir_power.call_args_list])
        self.assertIsInstance(h.error, AmbiguousWriteError)
        partial = [p for k, p in h.events if k == 'snapshot' and p['applied'] == {1: 51}]
        self.assertEqual(1, len(partial))
        self.assertLess(h.events.index(('snapshot', partial[0])), len(h.events) - 1)

    def assert_polling_without_save(self, h):
        with h.condition:
            reads = h.reads
            snapshots = sum(k == 'snapshot' for k, _ in h.events)
        h.wait(lambda: h.reads >= reads + 3 and
               sum(k == 'snapshot' for k, _ in h.events) >= snapshots + 3)
        h.adapter.apply_ir_power.assert_not_called()

    def test_blocked_initialization_continues_polling(self):
        h = Harness(block_init=True)
        self.addCleanup(h.close)
        h.start(wait_ready=False)
        h.wait(h.init_entered.is_set)
        self.assert_polling_without_save(h)
        h.init_release.set()
        h.wait_ready()
        h.fresh()
        h.release.set()
        h.wait(lambda: h.adapter.apply_ir_power.call_count == 1)

    def test_blocked_reset_continues_polling(self):
        h = self.harness(block_reset=True)
        h.fresh()
        h.options._target_temperature_setting.set(70)
        h.wait(lambda: h.group.state == 'resetting')
        h.release.set()
        h.wait(h.reset_entered.is_set)
        self.assert_polling_without_save(h)
        h.reset_release.set()
        h.wait(lambda: h.group.state == 'ready')
        self.assert_polling_without_save(h)
        self.assertEqual(1, len(h.calls))

    def test_image_handoff_discards_pending_features_and_waits_for_new_layer(self):
        h = self.harness(image=True, block_reset=True)
        h.fresh()
        self.assertEqual(10, h.calls[0][3]['center_coverage_pct'])
        h.options._slice_dataset_setting.request(make_dataset('replacement', 80))
        h.wait(lambda: any(k == 'slice_activated' and p['epoch'] == 1 for k, p in h.events))
        h.release.set()
        h.wait(h.reset_entered.is_set)
        self.assert_polling_without_save(h)
        h.reset_release.set()
        h.wait(lambda: h.group.state == 'ready')
        self.assert_polling_without_save(h)
        self.assertEqual(1, len(h.calls))
        h.fresh('after-image', 2)
        h.wait(lambda: h.adapter.apply_ir_power.call_count == 1)
        self.assertEqual(80, h.calls[1][3]['center_coverage_pct'])
        self.assertEqual('after-image', h.adapter.apply_ir_power.call_args.args[2])

    def test_context_changes_invalidate_pending_result(self):
        for change in ('settings', 'control_epoch', 'run'):
            with self.subTest(change=change):
                h = self.harness()
                h.fresh()
                with h.condition:
                    if change == 'settings':
                        h.frame = replace(h.frame, heater1_setting_percent=51)
                    elif change == 'control_epoch':
                        h.frame = replace(h.frame, control_epoch=1)
                    else:
                        h.frame = replace(h.frame, run_key='new-run')
                h.wait(lambda: h.group.state == 'resetting')
                h.release.set()
                h.wait(lambda: h.group.state == 'ready')
                self.assert_polling_without_save(h)
                self.assertEqual(1, len(h.calls))
                h.close()

    def test_hold_logged_without_terminating_controller(self):
        release = threading.Event()
        def runner(options, stop, emit):
            emit('connected', {})
            emit('calculation_hold', 'timeout diagnostic')
            release.wait(3)
        logger = Mock()
        controller = SessionController(runner, error_logger=logger)
        self.addCleanup(release.set)
        self.assertTrue(controller.connect(SessionOptions(TARGET)))
        deadline = time.monotonic() + 3
        events = []
        while time.monotonic() < deadline and not any(e.kind == 'calculation_hold' for e in events):
            events.extend(controller.poll())
            threading.Event().wait(.01)
        self.assertIn('calculation_hold', [e.kind for e in events])
        self.assertEqual('connected', controller.state)
        self.assertIsNone(controller.error)
        logger.assert_called_once_with('timeout diagnostic')
        controller.disconnect()
        release.set()
        controller.thread.join(3)


if __name__ == '__main__':
    unittest.main()
