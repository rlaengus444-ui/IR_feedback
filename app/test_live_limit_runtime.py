"""Live limit behavior through the real session and async workbook owner; no equipment."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from adapter_contracts import UiAdapter
from excel_async import AsyncWorkbookGroup, ExcelGroupFence
from session import SessionController, SessionOptions, run_ui_session
from test_excel_session_async import TARGET, SPEC, Harness


class LiveHarness:
    def __init__(self, *, initial=80, actual=80, coupled=True, block_process=False, block_save=False):
        self.condition = threading.Condition()
        self.current = replace(Harness.row('baseline', 0, coupled), heater1_setting_percent=actual,
                               heater2_setting_percent=actual)
        self.events = []
        self.reads = 0
        self.calls = []
        self.resets = 0
        self.process_entered, self.process_release = threading.Event(), threading.Event()
        self.save_entered, self.save_release = threading.Event(), threading.Event()
        self.block_process, self.block_save = block_process, block_save
        self.fence = ExcelGroupFence()
        self.group = None
        self.adapter = Mock(spec=UiAdapter)
        self.adapter.read_snapshot.side_effect = self.read
        self.adapter.apply_ir_power.side_effect = self.save
        self.options = SessionOptions(TARGET, mode='control', workbook_spec=SPEC, maximum_ir_percent=initial)
        h = self
        class FakeWorkbook:
            def __init__(self, spec, target, **kwargs): self.closed = False
            def set_target_temperature(self, value): pass
            def reset(self): h.resets += 1
            def process(self, layer, temperature, power, *, slice_features=None):
                with h.condition:
                    h.calls.append((layer, power))
                    h.condition.notify_all()
                h.process_entered.set()
                if h.block_process and not h.process_release.wait(4):
                    raise RuntimeError('test did not release process')
                return SimpleNamespace(power_percent=100, reason='synthetic')
            def close(self, force=False): self.closed = True
            def retirement_status(self): return {'verified': self.closed}
        self.factory = FakeWorkbook
        def group_factory(*args, **kwargs):
            h.group = AsyncWorkbookGroup(*args, fence=h.fence, **kwargs)
            return h.group
        def runner(options, stop, emit):
            run_ui_session(options, stop, emit, lambda: h.adapter, workbook_group_factory=group_factory)
        self.controller = SessionController(runner)

    def read(self, stop):
        with self.condition:
            self.reads += 1
            self.condition.notify_all()
            return self.current

    def save(self, heater, power, key, stop):
        self.save_entered.set()
        if self.block_save and heater == 1 and not self.save_release.wait(4):
            raise RuntimeError('test did not release save')
        with self.condition:
            field = 'heater1_setting_percent' if heater == 1 else 'heater2_setting_percent'
            updates = {field: power}
            if self.current.coupled_output:
                updates['heater2_setting_percent'] = power
            self.current = replace(self.current, **updates)
            self.condition.notify_all()

    def drain(self):
        self.events.extend(self.controller.poll())

    def wait(self, predicate, seconds=4):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.drain()
            if predicate(): return
            with self.condition: self.condition.wait(.02)
        self.drain()
        raise AssertionError((self.controller.state, self.events[-6:], self.calls))

    def start(self):
        self.patcher = patch('session.WorkbookController', self.factory)
        self.patcher.start()
        self.controller.connect(self.options)
        self.wait(lambda: (self.options.workbook_spec is None or
                          (self.group is not None and self.group.state == 'ready')) and
                  sum(e.kind == 'snapshot' for e in self.events) >= 2)
        return self

    def row(self, layer):
        with self.condition:
            self.current = replace(self.current, layer_index=layer, observation_key=f'row-{layer}')
            self.condition.notify_all()
        self.wait(lambda: any(e.kind == 'snapshot' and e.payload['snapshot'].observation_key == f'row-{layer}'
                              for e in self.events))

    def close(self):
        self.process_release.set()
        self.save_release.set()
        self.controller.disconnect()
        thread = self.controller.thread
        if thread:
            thread.join(5)
            if thread.is_alive(): raise AssertionError('session did not stop')
        self.patcher.stop()


class LiveLimitRuntimeTests(unittest.TestCase):
    def harness(self, **kwargs):
        h = LiveHarness(**kwargs)
        self.addCleanup(h.close)
        return h.start()

    def test_lower_limit_ramps_on_distinct_fresh_layers_only(self):
        h = self.harness()
        h.options._maximum_ir_setting.set(60)
        h.adapter.apply_ir_power.assert_not_called()
        for layer, expected in enumerate((75, 70, 65, 60), 1):
            h.row(layer)
            h.wait(lambda: h.adapter.apply_ir_power.call_count >= layer)
            self.assertEqual(expected, h.adapter.apply_ir_power.call_args.args[1])
            reads = h.reads
            h.wait(lambda: h.reads >= reads + 2)
            self.assertEqual(layer, h.adapter.apply_ir_power.call_count)
        self.assertEqual([f'row-{n}' for n in range(1, 5)],
                         [call.args[2] for call in h.adapter.apply_ir_power.call_args_list])
        self.assertTrue(all(len(call.args) == 4 for call in h.adapter.apply_ir_power.call_args_list))

    def test_increase_above_initial_limit_applies_on_fresh_observation(self):
        h = self.harness(initial=60, actual=60)
        h.options._maximum_ir_setting.set(80)
        h.adapter.apply_ir_power.assert_not_called()
        h.row(1)
        h.wait(lambda: h.adapter.apply_ir_power.call_count == 1)
        self.assertEqual(65, h.adapter.apply_ir_power.call_args.args[1])

    def test_fractional_actual_ramps_to_zero_without_overshoot(self):
        h = self.harness(initial=10, actual=7.5)
        h.options._maximum_ir_setting.set(0)
        for layer, expected in ((1, 2.5), (2, 0)):
            h.row(layer)
            h.wait(lambda: h.adapter.apply_ir_power.call_count >= layer)
            self.assertEqual(expected, h.adapter.apply_ir_power.call_args.args[1])
        self.assertEqual(0, h.current.heater1_setting_percent)

    def test_idle_cap_change_preserves_accepted_excel_history(self):
        h = self.harness(initial=80, actual=80)
        h.row(1)
        h.wait(lambda: any(e.kind == 'snapshot' and e.payload['snapshot'].observation_key == 'row-1'
                           and e.payload.get('proposed') == {1: 80} for e in h.events))
        h.options._maximum_ir_setting.set(60)
        reads = h.reads
        h.wait(lambda: h.reads >= reads + 2)
        self.assertEqual(0, h.resets)
        h.adapter.apply_ir_power.assert_not_called()
        h.row(2)
        h.wait(lambda: h.adapter.apply_ir_power.call_count == 1)
        self.assertEqual(75, h.adapter.apply_ir_power.call_args.args[1])
        self.assertEqual(0, h.resets)
        self.assertEqual([2, 3], [layer for layer, _ in h.calls])

    def test_json_minimum_rejection_and_custom_two_percent_step(self):
        with tempfile.TemporaryDirectory() as directory:
            config = json.loads(Path(__file__).with_name('workbook-replay.settings.json').read_text(encoding='utf-8'))
            config['parameters']['minimum_power_percent'] = 20
            config['parameters']['maximum_step_percent'] = 2
            path = Path(directory) / 'settings.json'
            path.write_text(json.dumps(config), encoding='utf-8')
            h = LiveHarness(initial=80, actual=80)
            h.options = SessionOptions(TARGET, mode='control', settings_path=path, maximum_ir_percent=80)
            self.addCleanup(h.close)
            h.start()
            stamp = h.options._maximum_ir_setting.snapshot()
            with self.assertRaises(ValueError):
                h.options._maximum_ir_setting.set(19.5)
            self.assertEqual(stamp, h.options._maximum_ir_setting.snapshot())
            self.assertEqual('connected', h.controller.state)
            h.options._maximum_ir_setting.set(60)
            h.adapter.apply_ir_power.assert_not_called()
            for layer, expected in ((1, 78), (2, 76)):
                h.row(layer)
                h.wait(lambda: h.adapter.apply_ir_power.call_count >= layer)
                self.assertEqual(expected, h.adapter.apply_ir_power.call_args.args[1])
            h.close()

    def test_cap_aba_while_excel_busy_discards_old_result(self):
        h = self.harness(block_process=True)
        h.row(1)
        h.wait(h.process_entered.is_set)
        h.options._maximum_ir_setting.set(60)
        h.options._maximum_ir_setting.set(80)
        h.wait(lambda: h.group.state == 'resetting')
        h.process_release.set()
        h.wait(lambda: h.group.state == 'ready' and h.resets >= 1)
        reads = h.reads
        h.wait(lambda: h.reads >= reads + 2)
        h.adapter.apply_ir_power.assert_not_called()
        h.row(2)
        h.wait(lambda: len(h.calls) >= 2)

    def test_basic_controller_cap_aba_during_batch_resets_both_histories(self):
        from layer_algorithm import LayerAdaptiveController
        with tempfile.TemporaryDirectory() as directory:
            config = json.loads(Path(__file__).with_name('workbook-replay.settings.json').read_text(encoding='utf-8'))
            config['parameters']['maximum_step_percent'] = 2
            path = Path(directory) / 'settings.json'
            path.write_text(json.dumps(config), encoding='utf-8')
            h = LiveHarness(initial=80, actual=90, coupled=False)
            h.options = SessionOptions(TARGET, mode='control', settings_path=path, maximum_ir_percent=80)
            h.current = replace(h.current, observation_is_new=False)
            self.addCleanup(h.close)
            instances, reset_lengths, processed = [], [], []
            class TrackingController(LayerAdaptiveController):
                def __init__(self, settings):
                    super().__init__(settings)
                    instances.append(self)
                def process(self, *args, **kwargs):
                    result = super().process(*args, **kwargs)
                    processed.append((self, args[0]))
                    if len(processed) == 1:
                        h.options._maximum_ir_setting.set(70)
                        h.options._maximum_ir_setting.set(80)
                    return result
                def reset(self):
                    previous_length = len(self._history)
                    super().reset()
                    reset_lengths.append((self, previous_length))
            with patch('session.LayerAdaptiveController', TrackingController):
                h.start()
                with h.condition:
                    h.current = replace(h.current, observation_is_new=True)
                h.row(1)
                h.wait(lambda: len(reset_lengths) >= 2)
                self.assertEqual(2, len(instances))
                self.assertEqual([1, 1], [size for _, size in reset_lengths[:2]])
                self.assertEqual(set(instances), {controller for controller, _ in reset_lengths[:2]})
                self.assertTrue(all(len(controller._history) == 0 for controller in instances), (processed, reset_lengths, [len(c._history) for c in instances]))
                reads = h.reads
                h.wait(lambda: h.reads >= reads + 3)
                h.adapter.apply_ir_power.assert_not_called()
                self.assertEqual(2, len(processed))
                h.row(2)
                h.wait(lambda: h.adapter.apply_ir_power.call_count == 2)
                self.assertEqual([88, 88], [call.args[1] for call in h.adapter.apply_ir_power.call_args_list])
                self.assertEqual(['row-2', 'row-2'], [call.args[2] for call in h.adapter.apply_ir_power.call_args_list])
                self.assertEqual([2, 2, 3, 3], [layer for _, layer in processed])
                h.close()

    def test_workbook_recovery_keeps_requested_limit(self):
        from test_excel_recovery_integration import RecoveryHarness
        h = RecoveryHarness()
        self.addCleanup(h.close)
        h.start()
        setting = h.options._maximum_ir_setting
        h.trigger_fault()
        h.wait(lambda: h.calc._recovery == 'backoff')
        setting.set(60)
        self.assertEqual(60, setting.snapshot()[1])
        h.advance(2)
        h.wait(lambda: len(h.instances) == 2 and h.calc._recovery is None)
        self.assertIs(setting, h.options._maximum_ir_setting)
        self.assertEqual(60, setting.snapshot()[1])
        h.adapter.apply_ir_power.assert_not_called()
        h.set_row('after-recovery', 2)
        h.wait(lambda: h.adapter.apply_ir_power.call_count == 1)
        self.assertLessEqual(h.adapter.apply_ir_power.call_args.args[1], 60)

    def test_change_between_heaters_keeps_first_save_and_blocks_second(self):
        h = self.harness(initial=90, actual=80, coupled=False, block_save=True)
        h.row(1)
        h.wait(h.save_entered.is_set)
        h.options._maximum_ir_setting.set(60)
        h.save_release.set()
        h.wait(lambda: any(e.kind == 'snapshot' and e.payload.get('applied') == {1: 85}
                           for e in h.events))
        self.assertEqual(1, h.adapter.apply_ir_power.call_count)
        self.assertEqual(1, h.adapter.apply_ir_power.call_args.args[0])


if __name__ == '__main__':
    unittest.main()
