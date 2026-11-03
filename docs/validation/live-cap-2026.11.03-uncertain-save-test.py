"""Offline synthetic admission/uncertain-Save regression, no COM or hardware."""
from dataclasses import replace
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from adapter_contracts import AmbiguousWriteError, UiSnapshot
from excel_async import AsyncWorkbookGroup, ExcelGroupFence
from layer_charts import LayerHistory
from observed_session import run_observed_session
from resilient_adapter import ResilientUiAdapter
from session import SessionController, SessionOptions, run_ui_session
from test_excel_session_async import TARGET, SPEC


class UncertainSaveTests(unittest.TestCase):
    def test_uncertain_second_save_stays_held_after_live_cap_and_fresh_rows(self):
        condition = threading.Condition()
        entered, release = threading.Event(), threading.Event()
        calls, events = [], []
        state = {'row': 0, 'power1': 75, 'power2': 75, 'gate': None}
        fence = ExcelGroupFence()
        class Raw:
            def __init__(self, source_factory): self.source_factory = source_factory
            def connect(self, target, stop): self.target = target
            def disconnect(self): pass
            def prepare_mode(self, mode, stop): pass
            def read_native_state(self, stop): return 'AUTO'
            def suspend_control(self, reason): pass
            def validate_control_epoch(self, expected): pass
            def set_apply_admission_gate(self, gate): state['gate'] = gate
            def read_snapshot(self, stop):
                with condition:
                    layer = state['row']
                    return UiSnapshot(TARGET, layer_index=layer, observation_key=f'row-{layer}',
                        run_key='job-a', csv_generation='source', observation_is_new=True,
                        observation_fresh=True, machine_state='AUTO', control_available=True,
                        heater1_setting_percent=state['power1'], heater2_setting_percent=state['power2'],
                        coupled_output=False, t_recoating_c=80, minimum_poll_seconds=.1)
            def apply_ir_power(self, heater, power, key, stop):
                state['gate']()
                with condition:
                    calls.append((heater, power, key))
                    if heater == 1:
                        state['power1'] = power
                    condition.notify_all()
                if heater == 2:
                    entered.set()
                    if not release.wait(4): raise RuntimeError('test did not release Save')
                    raise AmbiguousWriteError('Save acknowledgement uncertain')
        class Workbook:
            def __init__(self, spec, target, **kwargs): self.closed = False
            def set_target_temperature(self, value): pass
            def reset(self): pass
            def process(self, layer, temperature, power, *, slice_features=None):
                return SimpleNamespace(power_percent=80, reason='synthetic')
            def close(self, force=False): self.closed = True
            def retirement_status(self): return {'verified': self.closed}
        adapter = ResilientUiAdapter(raw_factory=Raw, source_factory=lambda target: SimpleNamespace(target=target))
        def runner(options, stop, emit, adapter_factory, *, preserve_adapter):
            return run_ui_session(options, stop, emit, adapter_factory, preserve_adapter=preserve_adapter,
                workbook_group_factory=lambda *a, **kw: AsyncWorkbookGroup(*a, fence=fence, **kw))
        def observed(options, stop, emit):
            return run_observed_session(options, stop, emit, adapter_factory=lambda: adapter,
                                        runner=runner, retry_delays=(.01,))
        logger = []
        controller = SessionController(observed, error_logger=logger.append)
        options = SessionOptions(TARGET, mode='control', workbook_spec=SPEC, maximum_ir_percent=80)
        def drain(): events.extend(controller.poll())
        def wait(predicate, seconds=5):
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                drain()
                if predicate(): return
                with condition: condition.wait(.025)
            self.fail(f'timed out; calls={calls!r}, events={events[-8:]!r}')
        def next_row(layer):
            with condition:
                state['row'] = layer
                condition.notify_all()
            wait(lambda: any(e.kind == 'snapshot' and e.payload['snapshot'].layer_index == layer for e in events))
        with patch('session.WorkbookController', Workbook):
            self.assertTrue(controller.connect(options))
            try:
                wait(lambda: any(e.kind == 'snapshot' for e in events))
                for layer in range(1, 8):
                    next_row(layer)
                    if entered.is_set(): break
                    deadline = time.monotonic() + .35
                    while not entered.is_set() and time.monotonic() < deadline:
                        drain()
                        with condition: condition.wait(.02)
                    if entered.is_set(): break
                self.assertTrue(entered.is_set())
                self.assertEqual([1, 2], [call[0] for call in calls])
                generation = controller.generation
                self.assertTrue(options._maximum_ir_setting.status()['in_flight'])
                self.assertEqual(60, options._maximum_ir_setting.set(60))
                release.set()
                wait(lambda: any(e.kind == 'actuation_barrier' for e in events))
                wait(lambda: any(e.kind == 'snapshot' and e.payload.get('applied') == {1: 80} for e in events))
                self.assertEqual(2, len(calls))
                self.assertTrue(any('AmbiguousWriteError' in entry for entry in logger))
                for later in range(state['row'] + 1, state['row'] + 5):
                    next_row(later)
                    matching = [e for e in events if e.kind == 'snapshot' and e.payload['snapshot'].layer_index == later]
                    self.assertTrue(matching[-1].payload['snapshot'].actuation_barrier)
                    self.assertFalse(matching[-1].payload['snapshot'].control_available)
                self.assertEqual(2, len(calls))
                self.assertEqual(60, options._maximum_ir_setting.snapshot()[1])
                self.assertEqual(generation, controller.generation)
                self.assertTrue(controller.worker_alive)
                history = LayerHistory()
                for event in events:
                    if event.kind == 'snapshot': history.record(event.payload)
                saved = [p for p in history.points if p.ir1_saved is not None or p.ir2_saved is not None]
                self.assertEqual(1, len(saved))
                self.assertEqual((80, None), (saved[0].ir1_saved, saved[0].ir2_saved))
            finally:
                release.set()
                controller.disconnect()
                if controller.thread:
                    controller.thread.join(5)
                    self.assertFalse(controller.thread.is_alive())


if __name__ == '__main__': unittest.main()
