import threading
import unittest
from dataclasses import replace

from adapter_contracts import (ReadTransportError, UiReadTimeout, ControlSuspended,
                               ProtocolError, UiSnapshot)
from resilient_adapter import ResilientUiAdapter
from test_resilient_adapter import TARGET, NEW, Reader, Raw


def failure(operation='validate', retired=True, actuation=False):
    return ReadTransportError(operation, 'offline', actuation_possible=actuation, helper_retired=retired)


class InitialPrepareRecoveryTests(unittest.TestCase):
    def make(self, errors, candidates=None):
        self.now = 0
        self.raws = []
        errors = list(errors)
        class ControlledRaw(Raw):
            def connect(raw, target, event):
                if errors:
                    error = errors.pop(0)
                    if error: raise error
                super().connect(target, event)
            def read_native_state(raw, event):
                if 'control' not in raw.modes:
                    raise ControlSuspended('audited state unavailable')
                return 'AUTO'
        def factory(source_factory):
            raw = ControlledRaw(source_factory); self.raws.append(raw); return raw
        self.adapter = ResilientUiAdapter(factory, lambda: candidates if candidates is not None else [TARGET], Reader, lambda: self.now)
        self.adapter.connect(TARGET, None)
        return self.adapter

    def reconnect(self):
        self.now += 61
        self.adapter._reconnect(None)

    def snap(self, key, layer):
        return UiSnapshot(target=TARGET, run_key='job', layer_index=layer, machine_state='AUTO',
                          observation_key=key, observation_is_new=True, observation_fresh=True,
                          control_available=True)

    def test_reconnect_prepares_once_then_baseline_and_fresh_layer(self):
        adapter = self.make([UiReadTimeout('timeout', 'validate', False)])
        reader = adapter._reader
        adapter.prepare_mode('control', None)
        self.assertTrue(adapter._initial_recovery_grant)
        self.reconnect()
        raw = self.raws[-1]
        self.assertIs(reader, raw.reader)
        self.assertEqual(['monitor', 'control'], raw.modes)
        self.assertFalse(adapter._initial_recovery_grant)
        raw.snapshot = self.snap('a', 1)
        self.assertFalse(adapter.read_snapshot(None).control_available)
        self.assertEqual(0, raw.calls)
        raw.snapshot = self.snap('b', 2)
        self.assertTrue(adapter.read_snapshot(None).control_available)
        self.assertEqual(0, raw.calls)

    def test_repeated_validated_failures_keep_one_shot(self):
        adapter = self.make([failure(), failure(), None])
        adapter.prepare_mode('control', None)
        self.reconnect()
        self.assertTrue(adapter._initial_recovery_grant)
        self.reconnect()
        self.assertEqual(['monitor', 'control'], self.raws[-1].modes)
        self.assertFalse(adapter._initial_recovery_grant)

    def test_revocation_cannot_be_recreated_on_same_object(self):
        for cause in ('stop', 'identity', 'unexpected'):
            with self.subTest(cause=cause):
                adapter = self.make([])
                # Simulate a pre-transport attempt while preserving the original selection.
                adapter._raw = None
                if cause == 'stop':
                    stop = threading.Event(); stop.set()
                    with self.assertRaises(Exception): adapter._check(stop)
                elif cause == 'identity':
                    with self.assertRaises(Exception): adapter.connect(NEW, None)
                else:
                    adapter._raw_factory = lambda source_factory: type('Bad', (), {
                        'connect': lambda self, *args: (_ for _ in ()).throw(RuntimeError('unknown')),
                        'disconnect': lambda self: None})()
                    with self.assertRaises(RuntimeError): adapter._attempt(TARGET, None)
                self.assertFalse(adapter._initial_prepare_allowed)
                adapter._raw_factory = lambda source_factory: type('Bad', (), {
                    'connect': lambda self, *args: (_ for _ in ()).throw(failure()),
                    'disconnect': lambda self: None})()
                adapter._attempt(TARGET, None)
                self.assertFalse(adapter._initial_recovery_grant)

    def test_factory_and_disconnect_failures_cannot_resurrect_grant(self):
        for stage in ('source', 'raw', 'disconnect'):
            with self.subTest(stage=stage):
                def explode(*args, **kwargs): raise RuntimeError(stage)
                source = explode if stage == 'source' else Reader
                class BadRaw:
                    def connect(self, *args): raise failure()
                    def disconnect(self):
                        if stage == 'disconnect': explode()
                factory = explode if stage == 'raw' else lambda source_factory: BadRaw()
                adapter = ResilientUiAdapter(factory, lambda: [TARGET], source, lambda: 0)
                with self.assertRaises(RuntimeError): adapter.connect(TARGET, None)
                self.assertFalse(adapter._initial_prepare_allowed)
                self.assertFalse(adapter._initial_recovery_grant)
                adapter._source_factory = Reader
                adapter._raw_factory = lambda source_factory: type('Bad', (), {
                    'connect': lambda self, *args: (_ for _ in ()).throw(failure()),
                    'disconnect': lambda self: None})()
                adapter.connect(TARGET, None)
                self.assertFalse(adapter._initial_recovery_grant)

    def test_monitor_prepare_validate_error_does_not_grant(self):
        class BadMonitor(Raw):
            def prepare_mode(self, mode, event): raise failure()
        adapter = ResilientUiAdapter(lambda source_factory: BadMonitor(source_factory),
                                     lambda: [TARGET], Reader, lambda: 0)
        adapter.connect(TARGET, None)
        self.assertFalse(adapter._initial_prepare_allowed)
        self.assertFalse(adapter._initial_recovery_grant)

    def test_each_original_identity_component_is_bound(self):
        alternatives = (replace(TARGET, pid=3), replace(TARGET, hwnd=4),
                        replace(TARGET, image_path=r'C:\other\BJT.exe'),
                        replace(TARGET, machine='S3'))
        for changed in alternatives:
            with self.subTest(changed=changed):
                adapter = self.make([failure()], [changed])
                adapter.prepare_mode('control', None)
                self.reconnect()
                self.assertFalse(adapter._initial_recovery_grant)
                if adapter._raw:
                    self.assertEqual(['monitor'], adapter._raw.modes)

    def test_discovery_failure_retries_only_when_retired_and_read_only(self):
        for exc, retained in ((failure('discover'), True),
                              (failure('discover', retired=False), False),
                              (failure('read_status'), False)):
            with self.subTest(error=exc, retained=retained):
                adapter = self.make([failure()])
                adapter.prepare_mode('control', None)
                adapter._discoverer = lambda: (_ for _ in ()).throw(exc)
                self.reconnect()
                self.assertEqual(retained, adapter._initial_recovery_grant)
                adapter._discoverer = lambda: [TARGET]
                self.reconnect()
                self.assertEqual(['monitor', 'control'] if retained else ['monitor'], self.raws[-1].modes)

    def test_sticky_fault_revokes_unused_grant(self):
        adapter = self.make([failure()])
        adapter.prepare_mode('control', None)
        adapter.freeze_control('uncertain')
        self.assertFalse(adapter._initial_recovery_grant)
        self.reconnect()
        self.assertEqual(['monitor'], self.raws[-1].modes)

    def test_unsafe_or_wrong_phase_never_grants(self):
        for exc in (failure('read_status'), failure(retired=False), failure(actuation=True),
                    OSError('io'), ProtocolError('bad')):
            with self.subTest(error=exc):
                adapter = self.make([exc])
                adapter.prepare_mode('control', None)
                self.assertFalse(adapter._initial_recovery_grant)
                self.reconnect()
                self.assertEqual(['monitor'], self.raws[-1].modes)

    def test_rebind_and_ambiguous_identity_revoke_grant(self):
        for candidates in ([NEW], [], [TARGET, TARGET]):
            with self.subTest(candidates=candidates):
                adapter = self.make([failure()], candidates)
                adapter.prepare_mode('control', None)
                self.reconnect()
                self.assertFalse(adapter._initial_recovery_grant)
                if adapter._raw:
                    self.assertEqual(['monitor'], self.raws[-1].modes)

    def test_monitor_preview_and_stop_do_not_prepare(self):
        for mode in ('monitor', 'preview'):
            with self.subTest(mode=mode):
                adapter = self.make([failure()])
                adapter.prepare_mode(mode, None)
                self.reconnect()
                self.assertNotIn('control', self.raws[-1].modes)
        adapter = self.make([failure()])
        adapter.prepare_mode('control', None)
        stop = threading.Event(); stop.set()
        with self.assertRaises(Exception): adapter._reconnect(stop)
        self.assertFalse(adapter._initial_recovery_grant)

    def test_grant_consumed_before_failed_preparation(self):
        adapter = self.make([failure()])
        adapter.prepare_mode('control', None)
        original_factory = adapter._raw_factory
        def factory(source_factory):
            raw = original_factory(source_factory)
            original = raw.prepare_mode
            def prepare(mode, event):
                if mode == 'control':
                    self.assertTrue(adapter._control_prepare_attempted)
                    self.assertFalse(adapter._initial_recovery_grant)
                    raise failure()
                original(mode, event)
            raw.prepare_mode = prepare
            return raw
        adapter._raw_factory = factory
        self.reconnect()
        self.assertFalse(adapter._initial_recovery_grant)
        self.assertTrue(adapter._control_prepare_attempted)
        adapter._raw_factory = original_factory
        self.reconnect()
        self.assertEqual(['monitor'], self.raws[-1].modes)

class InitialPrepareSessionTests(unittest.TestCase):
    def test_validate_timeout_retains_cap_and_requires_new_observations(self):
        import time
        from datetime import datetime
        from csv_source import CsvSample, CsvSnapshot, JobContext
        from observed_session import run_observed_session
        from session import SessionController, SessionOptions

        state = {'clock': 0, 'layer': 1, 'save': [], 'raws': [], 'connects': 0}
        class Source:
            def __init__(self, target): self.target = target
            def read(self):
                layer = state['layer']
                sample = CsvSample(layer, 80.0, '', datetime.now(), None, f'row-{layer}')
                return CsvSnapshot(sample, JobContext(run_key='job'), {'sha256': 'test'}, True, 'csv')
        class SyntheticRaw(Raw):
            def connect(self, target, event):
                state['connects'] += 1
                if state['connects'] == 1: raise UiReadTimeout('timeout', 'validate', False)
                super().connect(target, event)
            def set_apply_admission_gate(self, callback): self.gate = callback
            def read_native_state(self, event):
                if 'control' not in self.modes: raise ControlSuspended('audited handles unavailable')
                return 'AUTO'
            def read_snapshot(self, event):
                layer = state['layer']
                return UiSnapshot(target=TARGET, run_key='job', layer_index=layer,
                    machine_state='AUTO', observation_key=f'row-{layer}', observation_is_new=True,
                    observation_fresh=True, control_available=True, control_recoverable=True,
                    t_recoating_c=80, heater1_setting_percent=80, heater2_setting_percent=80,
                    minimum_poll_seconds=.05)
            def apply_ir_power(self, heater, power, key, stop):
                self.gate()
                state['save'].append((heater, power, key))
        def raw_factory(source_factory):
            raw = SyntheticRaw(source_factory); state['raws'].append(raw); return raw
        wrapper = ResilientUiAdapter(raw_factory, lambda: [TARGET], Source, lambda: state['clock'])
        options = SessionOptions(TARGET, mode='control', maximum_ir_percent=80)
        def runner(options, stop, emit):
            run_observed_session(options, stop, emit, adapter_factory=lambda: wrapper)
        controller = SessionController(runner)
        events = []
        def wait(predicate, timeout=8):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                events.extend(controller.poll())
                if predicate(): return
                time.sleep(.02)
            self.fail(f'timed out: {controller.state}, {events[-4:]}')
        try:
            controller.connect(options)
            wait(lambda: any(e.kind == 'snapshot' and e.payload['snapshot'].observation_key == 'row-1' for e in events))
            controller.update_maximum_ir(60)
            self.assertTrue(options._maximum_ir_setting.status()['pending'])
            self.assertEqual([], state['save'])
            self.assertTrue(wrapper._initial_recovery_grant)
            state['clock'] = 100
            wait(lambda: len(state['raws']) >= 2 and 'control' in state['raws'][-1].modes)
            wait(lambda: any(e.kind == 'snapshot' and e.payload['snapshot'].control_available is False and
                             e.payload['snapshot'].observation_key == 'row-1' for e in events))
            self.assertEqual([], state['save'])
            for layer in (2, 3, 4, 5):
                state['layer'] = layer
                wait(lambda: any(e.kind == 'snapshot' and e.payload['snapshot'].observation_key == f'row-{layer}' for e in events))
                if state['save']: break
            self.assertTrue(state['save'])
            self.assertTrue(all(key != 'row-1' and power <= 75 for _, power, key in state['save']))
            self.assertEqual(len(state['save']), len({(heater, key) for heater, _, key in state['save']}))
            self.assertEqual(60, options._maximum_ir_setting.snapshot()[1])
            seen = {e.payload['snapshot'].observation_key for e in events if e.kind == 'snapshot'}
            self.assertIn('row-1', seen)
            self.assertGreaterEqual(len(seen), 3)
        finally:
            controller.disconnect()
            if controller.thread: controller.thread.join(5)

if __name__ == '__main__': unittest.main()
