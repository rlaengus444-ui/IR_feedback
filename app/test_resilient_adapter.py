import unittest
from dataclasses import replace
from adapter_contracts import (MachineWindow, UiSnapshot, AmbiguousWriteError,
                               ControlSuspended, ReadTransportError)
from resilient_adapter import ResilientUiAdapter

TARGET = MachineWindow('M1', 1, 2, 'machine', r'C:\BJT\BJT.exe')
NEW = replace(TARGET, pid=3, hwnd=4)

class Reader:
    def __init__(self, target): self.target = target

class Raw:
    def __init__(self, source_factory):
        self.source_factory = source_factory
        self.snapshot = None
        self.calls = 0
        self.fail = False
        self.modes = []
        self.ambiguous = True
    def connect(self, target, event): self.target = target; self.reader = self.source_factory(target)
    def prepare_mode(self, mode, event): self.modes.append(mode)
    def read_native_state(self, event): return 'AUTO'
    def disconnect(self): pass
    def suspend_control(self, reason): pass
    def validate_control_epoch(self, expected): pass
    def read_snapshot(self, event):
        if self.fail: raise ReadTransportError('read_status', 'offline')
        return self.snapshot
    def apply_ir_power(self, *args):
        self.calls += 1
        if self.ambiguous: raise AmbiguousWriteError('unknown save')

class ResilienceTests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.raws = []
        def factory(source_factory):
            raw = Raw(source_factory); self.raws.append(raw); return raw
        self.adapter = ResilientUiAdapter(factory, lambda: [NEW], Reader, lambda: self.now)
        self.adapter.connect(TARGET, None)
        self.adapter.prepare_mode('control', None)
    def snap(self, key, layer=1, **kwargs):
        return UiSnapshot(target=TARGET, run_key='job', layer_index=layer,
            machine_state='AUTO', observation_key=key, observation_is_new=True,
            observation_fresh=True, control_available=True, **kwargs)
    def test_ambiguous_save_latches_and_rebind_preserves_identity(self):
        raw = self.raws[-1]
        raw.snapshot = self.snap('a')
        self.assertFalse(self.adapter.read_snapshot(None).control_available)
        raw.snapshot = self.snap('b', 2)
        self.assertTrue(self.adapter.read_snapshot(None).control_available)
        with self.assertRaises(ControlSuspended): self.adapter.apply_ir_power(1, 50, 'b', None)
        self.assertEqual(raw.calls, 1)
        raw.fail = True
        # Avoid needing a CSV fixture while disconnected.
        self.adapter._outage('offline')
        self.now = 3
        self.adapter._reconnect(None)
        replacement = self.raws[-1]
        replacement.snapshot = self.snap('c', 3)
        result = self.adapter.read_snapshot(None)
        self.assertEqual(result.target, TARGET)
        self.assertEqual(result.actual_target, NEW)
        self.assertFalse(result.control_available)
        self.assertIs(replacement.reader, raw.reader)
    def test_invalid_requires_baseline_then_another_fresh_sample(self):
        raw = self.raws[-1]
        raw.snapshot = self.snap('a')
        self.adapter.read_snapshot(None)
        raw.snapshot = self.snap('b', 2)
        self.assertTrue(self.adapter.read_snapshot(None).control_available)
        raw.snapshot = self.snap('bad', 3, measurement_valid=False, invalid_reason='sensor')
        self.assertFalse(self.adapter.read_snapshot(None).control_available)
        raw.snapshot = self.snap('c', 4)
        self.assertFalse(self.adapter.read_snapshot(None).control_available)
        raw.snapshot = self.snap('d', 5)
        self.assertTrue(self.adapter.read_snapshot(None).control_available)
    def test_uncertain_save_records_original_error_even_when_stop_is_set(self):
        import threading
        stop = threading.Event()
        errors = []
        self.adapter.set_fault_callback(errors.append)
        raw = self.raws[-1]
        raw.snapshot = self.snap('a', 1)
        self.adapter.read_snapshot(stop)
        raw.snapshot = self.snap('b', 2)
        self.adapter.read_snapshot(stop)
        original = AmbiguousWriteError('uncertain on close')
        def uncertain(*args):
            stop.set()
            raise original
        raw.apply_ir_power = uncertain
        with self.assertRaises(ControlSuspended):
            self.adapter.apply_ir_power(1, 50, 'b', stop)
        self.assertEqual(errors, [original])
        self.adapter.disconnect()
        self.assertEqual(errors, [original])

    def test_fault_registered_after_latch_delivers_original_once(self):
        from adapter_contracts import ProtocolError
        error = ProtocolError('bad helper response')
        self.adapter.freeze_control(error)
        errors = []
        self.adapter.set_fault_callback(errors.append)
        self.adapter.freeze_control(ProtocolError('later'))
        self.assertEqual(errors, [error])

    def test_baseline_requires_different_layer(self):
        raw = self.raws[-1]
        raw.snapshot = self.snap('first', 3)
        self.adapter.read_snapshot(None)
        raw.snapshot = self.snap('changed', 3)
        self.assertFalse(self.adapter.read_snapshot(None).control_available)
        raw.snapshot = self.snap('next', 4)
        self.assertTrue(self.adapter.read_snapshot(None).control_available)

    def test_independent_heaters_have_separate_high_water(self):
        raw = self.raws[-1]
        raw.ambiguous = False
        raw.snapshot = replace(self.snap('a', 1), coupled_output=False)
        self.adapter.read_snapshot(None)
        raw.snapshot = replace(self.snap('b', 2), coupled_output=False)
        self.adapter.read_snapshot(None)
        self.adapter.apply_ir_power(1, 50, 'b', None)
        self.adapter.apply_ir_power(2, 50, 'b', None)
        with self.assertRaises(ControlSuspended):
            self.adapter.apply_ir_power(1, 50, 'b', None)
        self.assertEqual(raw.calls, 2)

    def test_wrong_job_after_rebind_never_prepares_control(self):
        raw = self.raws[-1]
        raw.snapshot = self.snap('a')
        self.adapter.read_snapshot(None)
        self.adapter._outage('offline')
        self.now = 3
        self.adapter._reconnect(None)
        raw = self.raws[-1]
        raw.snapshot = replace(self.snap('other', 2), run_key='other')
        self.assertFalse(self.adapter.read_snapshot(None).control_available)
        self.assertEqual(raw.modes, ['monitor'])

    def test_running_state_authorizes_job(self):
        raw = self.raws[-1]
        raw.snapshot = replace(self.snap('a', 1), machine_state='RUNNING')
        self.adapter.read_snapshot(None)
        raw.snapshot = replace(self.snap('b', 2), machine_state='RUNNING')
        self.assertTrue(self.adapter.read_snapshot(None).control_available)

    def test_temporary_settings_read_failure_recovers(self):
        from sequence_persistence import PersistenceUnavailable
        raw = self.raws[-1]
        raw.snapshot = self.snap('a', 1)
        self.adapter.read_snapshot(None)
        raw.snapshot = self.snap('b', 2)
        self.assertTrue(self.adapter.read_snapshot(None).control_available)
        read = raw.read_snapshot
        raw.read_snapshot = lambda event: (_ for _ in ()).throw(PersistenceUnavailable('busy'))
        self.adapter._offline = lambda: UiSnapshot(target=TARGET)
        held = self.adapter.read_snapshot(None)
        self.assertFalse(held.control_available)
        self.assertFalse(held.actuation_barrier)
        raw.read_snapshot = read
        raw.snapshot = self.snap('c', 3)
        self.assertFalse(self.adapter.read_snapshot(None).control_available)
        raw.snapshot = self.snap('d', 4)
        self.assertTrue(self.adapter.read_snapshot(None).control_available)

    def test_temporary_settings_failure_during_prepare_retries(self):
        from sequence_persistence import PersistenceUnavailable
        raw = self.raws[-1]
        raw.prepare_mode = lambda mode, event: (_ for _ in ()).throw(PersistenceUnavailable('busy'))
        self.adapter._prepared_mode = 'monitor'
        self.adapter._offline = lambda: UiSnapshot(target=TARGET)
        raw.snapshot = self.snap('a', 1)
        self.adapter.read_snapshot(None)
        self.assertFalse(self.adapter._sticky)
        self.assertIsNone(self.adapter._raw)
        self.now = 3
        self.adapter._reconnect(None)
        self.assertIsNotNone(self.adapter._raw)

    def test_stale_first_job_remains_pinned_after_outage(self):
        raw = self.raws[-1]
        raw.snapshot = replace(self.snap('old', 1), observation_fresh=False,
            observation_key=None, t_recoating_c=None, run_key='job-a')
        self.assertFalse(self.adapter.read_snapshot(None).control_available)
        self.assertEqual(self.adapter._pinned_job, 'job-a')
        self.adapter._outage('offline')
        self.now = 3
        self.adapter._reconnect(None)
        raw = self.raws[-1]
        raw.snapshot = replace(self.snap('new', 2), run_key='job-b')
        self.assertFalse(self.adapter.read_snapshot(None).control_available)
        self.assertEqual(raw.modes, ['monitor'])
        self.assertEqual(self.adapter._pinned_job, 'job-a')

    def test_rebind_prep_uses_native_auto_even_if_log_state_unknown(self):
        raw = self.raws[-1]
        raw.snapshot = self.snap('old', 1)
        self.adapter.read_snapshot(None)
        self.adapter._outage('offline')
        self.now = 3
        self.adapter._reconnect(None)
        raw = self.raws[-1]
        raw.snapshot = replace(self.snap('new', 2), machine_state='unknown')
        self.adapter.read_snapshot(None)
        self.assertEqual(raw.modes, ['monitor', 'control'])

    def test_hold_reason_identifies_actual_cause(self):
        raw = self.raws[-1]
        raw.snapshot = self.snap('a')
        initial = self.adapter.read_snapshot(None)
        self.assertEqual(initial.control_unavailable_reason, '새 기준 및 새 관측 대기')
        raw.snapshot = self.snap('b', 2)
        self.assertTrue(self.adapter.read_snapshot(None).control_available)
        raw.snapshot = replace(self.snap('c', 3), control_available=False,
            control_unavailable_reason='설정 저장 파일 확인 대기')
        self.assertEqual(self.adapter.read_snapshot(None).control_unavailable_reason, '설정 저장 파일 확인 대기')
        raw.snapshot = replace(self.snap('bad', 4), measurement_valid=False, invalid_reason='온도계 비활성')
        self.assertEqual(self.adapter.read_snapshot(None).control_unavailable_reason, '온도계 비활성')
        raw.snapshot = replace(self.snap('missing', 5), run_key=None)
        self.assertIn('작업 식별', self.adapter.read_snapshot(None).control_unavailable_reason)
        raw.snapshot = replace(self.snap('other', 6), run_key='another')
        self.assertIn('작업과 달라', self.adapter.read_snapshot(None).control_unavailable_reason)
        self.assertFalse(self.adapter._last_snapshot.control_available)

    def test_log_read_unavailable_preserves_specific_reason(self):
        from csv_source import CsvReadUnavailable
        raw = self.raws[-1]
        raw.read_snapshot = lambda event: (_ for _ in ()).throw(CsvReadUnavailable('원본 로그 읽기 중'))
        result = self.adapter.read_snapshot(None)
        self.assertEqual(result.control_unavailable_reason, '원본 로그 읽기 중')
        self.assertFalse(result.control_available)

    def test_other_job_never_armed(self):
        raw = self.raws[-1]
        raw.snapshot = self.snap('a')
        self.adapter.read_snapshot(None)
        raw.snapshot = replace(self.snap('b', 2), run_key='other')
        self.assertFalse(self.adapter.read_snapshot(None).control_available)

class SessionRecoveryTests(unittest.TestCase):
    def test_session_resumes_saving_after_rebind(self):
        from itertools import count
        from session import SessionOptions, run_ui_session
        class Stop:
            stopped = False
            def is_set(self): return self.stopped
            def set(self): self.stopped = True
            def wait(self, timeout): return self.stopped
        stop = Stop()
        frames = count()
        saves = []
        raw_instances = []
        class ScriptedRaw(Raw):
            def read_snapshot(self, event):
                index = next(frames)
                if index == 6:
                    raise ReadTransportError('read_status', 'offline')
                return UiSnapshot(target=self.target, run_key='job', layer_index=index,
                    t_recoating_c=65, heater1_setting_percent=50, heater2_setting_percent=50,
                    machine_state='AUTO', observation_key=str(index), observation_is_new=True,
                    observation_fresh=True, control_available=True, minimum_poll_seconds=0)
            def apply_ir_power(self, heater, power, key, event):
                saves.append((len(raw_instances), key))
        def factory(source_factory):
            raw = ScriptedRaw(source_factory)
            raw_instances.append(raw)
            return raw
        ticks = count(step=100)
        adapter = ResilientUiAdapter(factory, lambda: [NEW], Reader, lambda: next(ticks))
        adapter._offline = lambda: UiSnapshot(target=TARGET, machine_state='unknown',
            control_epoch=adapter._epoch, control_recoverable=True, minimum_poll_seconds=0)
        reports = []
        def emit(kind, payload):
            if kind == 'snapshot':
                reports.append(payload)
                if len(reports) >= 17: stop.set()
        run_ui_session(SessionOptions(TARGET, mode='control', target_temperature_c=80),
                       stop, emit, lambda: adapter)
        self.assertGreaterEqual(len(raw_instances), 2)
        self.assertTrue(any(instance >= 2 for instance, _ in saves), (saves, reports))

class HelperRetirementTests(unittest.TestCase):
    def test_non_ir_prepare_timeout_is_recoverable_when_helper_retires(self):
        import os
        import subprocess
        from itertools import count
        from types import SimpleNamespace
        from unittest.mock import patch
        from windows_ui_adapter import WindowsUiAdapter
        class Retirable:
            dead = False
            def poll(self): return 0 if self.dead else None
            def kill(self): self.dead = True
            def communicate(self, payload=None, timeout=None):
                if self.dead: return '', ''
                raise subprocess.TimeoutExpired('fake', timeout)
        for operation in ('enter_sequence', 'prepare_control'):
            with self.subTest(operation=operation):
                child = Retirable()
                ticks = count(step=10)
                with patch('windows_ui_adapter.os', SimpleNamespace(name='nt', environ=os.environ)), \
                     patch('windows_ui_adapter.time', SimpleNamespace(monotonic=lambda: next(ticks))), \
                     patch('windows_ui_adapter.subprocess.Popen', return_value=child):
                    with self.assertRaises(ReadTransportError) as failure:
                        WindowsUiAdapter(helper_timeout=.1)._request({'operation': operation}, None)
                self.assertFalse(failure.exception.actuation_possible)
                self.assertTrue(failure.exception.helper_retired)
                self.assertTrue(child.dead)

    def test_live_helper_fences_all_new_helpers_and_uncertain_write_latches(self):
        import os
        import subprocess
        from itertools import count
        from types import SimpleNamespace
        from unittest.mock import patch
        from windows_ui_adapter import WindowsUiAdapter
        class LiveHelper:
            dead = False
            def poll(self): return 0 if self.dead else None
            def kill(self): raise OSError('denied')
            def communicate(self, payload=None, timeout=None):
                raise subprocess.TimeoutExpired('fake', timeout)
        child = LiveHelper()
        ticks = count(step=10)
        fake_os = SimpleNamespace(name='nt', environ=os.environ)
        fake_time = SimpleNamespace(monotonic=lambda: next(ticks))
        transport = WindowsUiAdapter(helper_timeout=.1)
        try:
            with patch('windows_ui_adapter.os', fake_os), patch('windows_ui_adapter.time', fake_time), \
                 patch('windows_ui_adapter.subprocess.Popen', return_value=child) as popen:
                with self.assertRaises(AmbiguousWriteError):
                    transport._request({'operation': 'apply'}, None)
                with self.assertRaises(ReadTransportError) as failure:
                    WindowsUiAdapter(helper_timeout=.1)._request({'operation': 'discover'}, None)
                self.assertFalse(failure.exception.helper_retired)
                self.assertEqual(popen.call_count, 1)
                child.dead = True
                old_directory = WindowsUiAdapter._unretired_directory
                WindowsUiAdapter._unretired_helper = None
                WindowsUiAdapter._unretired_directory = None
                if old_directory: old_directory.cleanup()
                child.dead = False
                with self.assertRaises(ReadTransportError) as failure:
                    transport._request({'operation': 'read_status'}, None)
                self.assertFalse(failure.exception.helper_retired)
                with self.assertRaises(ReadTransportError):
                    WindowsUiAdapter(helper_timeout=.1)._request({'operation': 'discover'}, None)
                self.assertEqual(popen.call_count, 2)
        finally:
            child.dead = True
            WindowsUiAdapter._unretired_helper = None
            directory = WindowsUiAdapter._unretired_directory
            WindowsUiAdapter._unretired_directory = None
            if directory: directory.cleanup()

if __name__ == '__main__': unittest.main()
