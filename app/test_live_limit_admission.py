"""Admission checks use synthetic files, helpers and native responses only."""
import os
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from adapter_contracts import ObservationSuspended, ReadTransportError, ControlSuspended, UiSnapshot
from resilient_adapter import ResilientUiAdapter
import test_windows_ui_adapter as fixture_module
from test_resilient_adapter import Raw, Reader, TARGET, NEW
from windows_ui_adapter import WindowsUiAdapter


class LimitChanged(ObservationSuspended):
    pass


class AdmissionTests(unittest.TestCase):
    def test_lowered_during_real_preflight_denies_before_edit(self):
        with tempfile.TemporaryDirectory() as directory:
            raw, state, calls, path, values, append, baseline, source, audit = fixture_module.AdapterTests.fixture(self, directory)
            snapshot = append()
            cap = [70]
            checks = []
            def gate():
                checks.append(cap[0])
                if cap[0] < 67: raise LimitChanged('limit changed')
            raw.set_apply_admission_gate(gate)
            state['hook'] = lambda request: cap.__setitem__(0, 60)
            with self.assertRaises(LimitChanged):
                raw.apply_ir_power(1, 67, snapshot.observation_key, threading.Event())
            self.assertEqual(checks, [60])
            self.assertEqual(state['saves'], 0)
            self.assertFalse(any(request['operation'] == 'apply' for request in calls))
            self.assertIsNone(raw._control_fault)

    def test_change_after_gate_does_not_repeat_or_cancel_admitted_save(self):
        with tempfile.TemporaryDirectory() as directory:
            raw, state, calls, path, values, append, baseline, source, audit = fixture_module.AdapterTests.fixture(self, directory)
            snapshot = append()
            cap, checks = [70], []
            raw.set_apply_admission_gate(lambda: checks.append(cap[0]))
            original = raw._runner
            def runner(request, stop):
                if request['operation'] == 'apply': cap[0] = 60
                return original(request, stop)
            raw._runner = runner
            raw.apply_ir_power(1, 67, snapshot.observation_key, threading.Event())
            self.assertEqual(checks, [70])
            self.assertEqual(state['saves'], 1)
            self.assertEqual(sum(c['operation'] == 'apply' for c in calls), 1)

    def test_denied_created_helper_is_reaped_without_payload(self):
        class Child:
            killed = False
            payloads = []
            def poll(self): return 0 if self.killed else None
            def kill(self): self.killed = True
            def communicate(self, payload=None, timeout=None):
                self.payloads.append(payload)
                return '', ''
        child = Child()
        raw = WindowsUiAdapter(helper_timeout=.1)
        def denied(): raise LimitChanged('changed')
        raw.set_apply_admission_gate(denied)
        with patch('windows_ui_adapter.os', SimpleNamespace(name='nt', environ=os.environ)), \
             patch('windows_ui_adapter.subprocess.Popen', return_value=child):
            with self.assertRaises(LimitChanged):
                raw._request({'operation': 'apply'}, None)
        self.assertTrue(child.killed)
        self.assertTrue(all(payload is None for payload in child.payloads))
        self.assertIsNone(raw._active_helper)

    def test_denied_unretired_helper_fences_without_ambiguous_write(self):
        import subprocess
        class Child:
            dead = False
            payloads = []
            def poll(self): return 0 if self.dead else None
            def kill(self): raise OSError('cannot kill')
            def communicate(self, payload=None, timeout=None):
                self.payloads.append(payload)
                raise subprocess.TimeoutExpired('fake', timeout)
        child = Child()
        raw = WindowsUiAdapter(helper_timeout=.1)
        raw.set_apply_admission_gate(lambda: (_ for _ in ()).throw(LimitChanged('changed')))
        try:
            with patch('windows_ui_adapter.os', SimpleNamespace(name='nt', environ=os.environ)), \
                 patch('windows_ui_adapter.subprocess.Popen', return_value=child) as popen:
                with self.assertRaises(ReadTransportError) as failure:
                    raw._request({'operation': 'apply'}, None)
                self.assertFalse(failure.exception.actuation_possible)
                self.assertFalse(failure.exception.helper_retired)
                self.assertTrue(all(payload is None for payload in child.payloads))
                with self.assertRaises(ReadTransportError):
                    WindowsUiAdapter()._request({'operation': 'discover'}, None)
                self.assertEqual(popen.call_count, 1)
                preedit_error = failure.exception
                wrapper = ResilientUiAdapter(lambda source_factory: Raw(source_factory),
                    lambda: [NEW], Reader, lambda: 0)
                wrapper.connect(TARGET, None)
                wrapper._last_snapshot = UiSnapshot(TARGET, layer_index=1, run_key='job',
                    observation_key='row', control_available=True)
                def fail_preedit(*args): raise preedit_error
                wrapper._raw.apply_ir_power = fail_preedit
                with self.assertRaises(ControlSuspended):
                    wrapper.apply_ir_power(1, 50, 'row', None)
                self.assertEqual(wrapper._sticky, '')
        finally:
            child.dead = True
            WindowsUiAdapter._unretired_helper = None
            directory = WindowsUiAdapter._unretired_directory
            WindowsUiAdapter._unretired_directory = None
            if directory: directory.cleanup()

    def test_gate_runs_once_even_if_communication_polls(self):
        import subprocess
        class Child:
            dead = False
            received = []
            def poll(self): return 0 if self.dead else None
            def kill(self): self.dead = True
            def communicate(self, payload=None, timeout=None):
                if self.dead: return '', ''
                self.received.append(payload)
                if len(self.received) == 1:
                    raise subprocess.TimeoutExpired('fake', timeout)
                self.dead = True
                return '{"success":true}', ''
        child = Child()
        calls = []
        raw = WindowsUiAdapter(helper_timeout=.1)
        raw.set_apply_admission_gate(lambda: calls.append('admitted'))
        with patch('windows_ui_adapter.os', SimpleNamespace(name='nt', environ=os.environ)), \
             patch('windows_ui_adapter.subprocess.Popen', return_value=child):
            raw._request({'operation': 'apply'}, None)
        self.assertEqual(calls, ['admitted'])
        self.assertIsNotNone(child.received[0])
        self.assertIsNone(child.received[1])

    def test_forwarding_survives_rebind_and_nonapply_does_not_gate(self):
        raws = []
        class GateRaw(Raw):
            def set_apply_admission_gate(self, gate): self.gate = gate
        def factory(source_factory):
            raw = GateRaw(source_factory); raws.append(raw); return raw
        wrapper = ResilientUiAdapter(factory, lambda: [NEW], Reader, lambda: 100)
        gate = lambda: None
        wrapper.set_apply_admission_gate(gate)
        wrapper.connect(TARGET, None)
        self.assertIs(raws[-1].gate, gate)
        wrapper.drop_transport()
        wrapper._reconnect(None)
        self.assertIs(raws[-1].gate, gate)
        wrapper.set_apply_admission_gate(None)
        self.assertIsNone(raws[-1].gate)
        raw = WindowsUiAdapter(runner=lambda request, stop: {'success': True})
        raw.set_apply_admission_gate(lambda: self.fail('read must not be gated'))
        raw._request({'operation': 'verify_control'}, None)
        raw._request({'operation': 'read_status'}, None)


if __name__ == '__main__': unittest.main()
