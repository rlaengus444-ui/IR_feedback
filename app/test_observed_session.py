"""Offline coverage for the persistent observer-only recovery wrapper."""
import threading
import unittest
import json
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

from observed_session import run_observed_session
from session import SessionController, SessionOptions, run_ui_session
from adapter_contracts import AmbiguousWriteError
from test_session import TARGET, snapshot
from test_slice_handoff import A, B


class FastStop:
    def __init__(self, stop_after):
        self.stop_after = stop_after
        self.delays = []
        self.stopped = False

    def is_set(self):
        return self.stopped

    def wait(self, delay):
        self.delays.append(delay)
        if len(self.delays) >= self.stop_after:
            self.stopped = True
        return self.stopped


class ObservedSessionTests(unittest.TestCase):
    def test_invalid_control_bounds_remain_unbound_in_monitor_fallback(self):
        document = json.loads(Path(__file__).with_name('workbook-replay.settings.json').read_text())
        document['parameters']['minimum_power_percent'] = 50
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'settings.json'
            path.write_text(json.dumps(document))
            options = SessionOptions(TARGET, mode='control', settings_path=path, maximum_ir_percent=49)
            adapter = Mock()
            adapter.read_snapshot.return_value = snapshot('base', 0)
            stop = threading.Event()
            events = []
            def emit(kind, payload):
                events.append((kind, payload))
                if kind == 'snapshot':
                    stop.set()
            run_observed_session(options, stop, emit, adapter_factory=lambda: adapter,
                                 runner=run_ui_session, retry_delays=(0,))
            self.assertTrue(any(kind == 'observer_hold' for kind, _ in events))
            self.assertFalse(options._maximum_ir_setting.status()['ready'])
            with self.assertRaises(ValueError):
                options._maximum_ir_setting.set(60)
            adapter.apply_ir_power.assert_not_called()

    def test_bound_limit_survives_monitor_fallback(self):
        options = SessionOptions(TARGET, mode='control', maximum_ir_percent=60)
        options._maximum_ir_setting.bind_bounds(50, 80, 2)
        adapter, stop = Mock(), threading.Event()
        adapter.read_snapshot.return_value = snapshot('base', 0)
        def runner(current, stopped, emit, factory, *, preserve_adapter):
            if current.mode == 'control':
                raise ValueError('configuration fault after binding')
            return run_ui_session(current, stopped, emit, factory, preserve_adapter=preserve_adapter)
        def emit(kind, value):
            if kind == 'snapshot':
                stop.set()
        run_observed_session(options, stop, emit, adapter_factory=lambda: adapter,
                             runner=runner, retry_delays=(0,))
        status = options._maximum_ir_setting.status()
        self.assertEqual((50, 80, 2), (status['minimum'], status['hard_maximum'], status['step']))
        self.assertEqual(70, options._maximum_ir_setting.set(70))
        adapter.apply_ir_power.assert_not_called()

    def test_barrier_is_logged_before_queue_even_during_close(self):
        import observed_session
        adapter = Mock()
        entered, released = threading.Event(), threading.Event()
        original = AmbiguousWriteError('Save result unknown')
        original.__cause__ = OSError('pipe closed')
        def runner(options, stop, emit, factory, *, preserve_adapter):
            entered.set()
            released.wait(1)
            adapter.set_fault_callback.call_args.args[0](original)
        def wrapper(options, stop, emit):
            run_observed_session(options, stop, emit, adapter_factory=lambda: adapter,
                                 runner=runner, retry_delays=(0,))
        logger = Mock()
        with patch.object(observed_session, 'run_observed_session', wrapper):
            controller = SessionController(error_logger=logger)
        controller.connect(SessionOptions(TARGET, mode='control'))
        self.assertTrue(entered.wait(1))
        controller.close()
        released.set()
        controller.thread.join(1)
        self.assertEqual([], controller.poll())
        logger.assert_called_once()
        self.assertIn('AmbiguousWriteError: Save result unknown', logger.call_args.args[0])
        self.assertIn('OSError: pipe closed', logger.call_args.args[0])

    def test_faults_stay_monitor_only_and_preserve_shared_settings(self):
        options = SessionOptions(TARGET, mode='control', slice_dataset=A)
        adapter = Mock()
        stop = FastStop(7)
        attempts, events = [], []
        def runner(current, stopped, emit, factory, *, preserve_adapter):
            self.assertIs(factory(), adapter)
            self.assertTrue(preserve_adapter)
            attempts.append(current)
            self.assertIs(current._target_temperature_setting, options._target_temperature_setting)
            self.assertIs(current._slice_dataset_setting, options._slice_dataset_setting)
            if len(attempts) == 1:
                current._target_temperature_setting.set(72)
                current._slice_dataset_setting.request(B)
                raise ValueError('invalid algorithm configuration')
            self.assertEqual(current._target_temperature_setting.get(), 72)
            self.assertEqual(current._slice_dataset_setting.desired()[1], B)
            raise RuntimeError('observation read failed')
        run_observed_session(options, stop, lambda k, v: events.append((k, v)),
                             adapter_factory=lambda: adapter, runner=runner,
                             retry_delays=(2, 5, 15, 30, 60))
        self.assertEqual([2, 5, 15, 30, 60, 60, 60], stop.delays)
        self.assertEqual(['control'] + ['monitor'] * 6, [item.mode for item in attempts])
        self.assertEqual(7, adapter.freeze_control.call_count)
        self.assertEqual(7, sum(kind == 'observer_hold' for kind, _ in events))
        self.assertNotIn('finished', [kind for kind, _ in events])
        adapter.disconnect.assert_called_once()

    def test_stop_during_backoff_wakes_without_another_attempt(self):
        stop = threading.Event()
        holding = threading.Event()
        adapter = Mock()
        runner = Mock(side_effect=ValueError('bad configuration'))
        def emit(kind, value):
            if kind == 'observer_hold':
                holding.set()
        thread = threading.Thread(target=run_observed_session,
                                  args=(SessionOptions(TARGET, mode='preview'), stop, emit),
                                  kwargs=dict(adapter_factory=lambda: adapter, runner=runner,
                                              retry_delays=(60,)), daemon=True)
        thread.start()
        self.assertTrue(holding.wait(1))
        stop.set()
        thread.join(1)
        self.assertFalse(thread.is_alive())
        runner.assert_called_once()
        adapter.disconnect.assert_called_once()

    def test_default_session_controller_keeps_generation_until_stop(self):
        import observed_session
        adapter = Mock()
        resumed = threading.Event()
        attempts = []
        def runner(options, stop, emit, factory, *, preserve_adapter):
            attempts.append(options.mode)
            if options.mode != 'monitor':
                emit('connected', {})
                emit('snapshot', {'snapshot': snapshot('before', 0), 'applied': {}})
                raise ValueError('bad settings')
            emit('connected', {})
            emit('snapshot', {'snapshot': snapshot('after', 1), 'applied': {}})
            resumed.set()
            stop.wait(2)
        def wrapper(options, stop, emit):
            return run_observed_session(options, stop, emit, adapter_factory=lambda: adapter,
                                        runner=runner, retry_delays=(0,))
        with patch.object(observed_session, 'run_observed_session', wrapper):
            controller = SessionController()
        options = SessionOptions(TARGET, mode='control', slice_dataset=A)
        self.assertTrue(controller.connect(options))
        try:
            self.assertTrue(resumed.wait(1))
            generation = controller.generation
            events = controller.poll()
            self.assertEqual(['control', 'monitor'], attempts)
            self.assertEqual(1, generation)
            self.assertTrue(all(event.generation == generation for event in events))
            self.assertEqual(2, sum(event.kind == 'snapshot' for event in events))
            self.assertNotIn('finished', [event.kind for event in events])
            self.assertEqual('connected', controller.state)
            self.assertIs(controller.target_temperature_setting, options._target_temperature_setting)
            self.assertIs(controller.slice_dataset_setting, options._slice_dataset_setting)
        finally:
            controller.disconnect()
            controller.thread.join(1)
        finished = controller.poll()
        self.assertEqual(['finished'], [event.kind for event in finished])
        self.assertEqual(generation, controller.generation)
        self.assertEqual('disconnected', controller.state)
        adapter.disconnect.assert_called_once()


if __name__ == '__main__':
    unittest.main()
