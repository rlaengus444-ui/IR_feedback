"""Offline tests for recoverable control holds and resumption."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
from adapter_contracts import AdapterError, AmbiguousWriteError, ControlSuspended, TargetIdentityError, UiReadTimeout
import sequence_persistence
from sequence_field_audit import SequenceFieldAuditError
import test_windows_ui_adapter
import test_session
from session import SessionOptions, run_ui_session

class RecoveryAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        (self.adapter, self.state, self.calls, self.path, self.values, self.append,
         self.baseline, self.source, self.audit) = test_windows_ui_adapter.AdapterTests.fixture(self, self.temp.name)
        self.now = [time.monotonic() + 10]
        self.time_patch = patch('windows_ui_adapter.time.monotonic', side_effect=lambda: self.now[0])
        self.time_patch.start()
        self.addCleanup(self.time_patch.stop)
        self.original_runner = self.adapter._runner
        self.recovery = {'machine': 'M1', 'state': 'AUTO', 'version': 1, 'error': None, 'hook': None}
        def runner(request, stop):
            if request['operation'] != 'recover_control':
                return self.original_runner(request, stop)
            self.calls.append(request)
            if self.recovery['hook']:
                self.recovery['hook']()
            if self.recovery['error']:
                raise self.recovery['error']
            fields = {'textBoxHeatingOutput': self.state['power'], 'textBoxSpeed': self.state['speed'], 'checkBoxHeat': 'toggle:1'}
            mapping = {name: {'hwnd': 100 + i + self.recovery['version'], 'kind': 'toggle' if name == 'checkBoxHeat' else 'text'} for i, name in enumerate(fields)}
            return {'success': True, 'machine': self.recovery['machine'], 'state': self.recovery['state'], 'fields': fields, 'field_map': mapping, 'setting': self.state['power'], 'handles': {'scope': 10, 'ir': 11, 'save': 12, 'state': 99}}
        self.adapter._runner = runner

    def poll(self, step=1.1):
        self.now[0] += step
        self.adapter._last_read = 0
        return self.adapter.read_snapshot(threading.Event())

    def persist(self, power=None, speed=None):
        if power is not None:
            self.values[0]['Value'] = str(power)
            self.state['power'] = str(power)
        if speed is not None:
            self.values[1]['Value'] = str(speed)
            self.state['speed'] = str(speed)
        self.path.write_text(json.dumps(self.values), encoding='utf-8')

    def test_saved_ir_and_non_ir_change_stable_two_checks_then_save(self):
        self.persist(power=64, speed=6)
        start = len(self.calls)
        held = self.poll()
        self.assertFalse(held.control_available)
        self.assertTrue(held.control_recoverable)
        self.assertEqual(0, held.control_epoch)
        ready = self.poll()
        self.assertTrue(ready.control_available)
        self.assertEqual((1, 64), (ready.control_epoch, ready.heater1_setting_percent))
        self.assertNotIn('enter_sequence', [c['operation'] for c in self.calls[start:]])
        self.assertEqual(2, sum(c['operation'] == 'recover_control' for c in self.calls[start:]))
        fresh = self.append(index=1)
        self.adapter.apply_ir_power(1, 67, fresh.observation_key, threading.Event())
        self.assertEqual(1, self.state['saves'])
        self.assertTrue(self.poll().control_available)

    def test_unverified_ui_and_unavailable_persistence_stay_on_hold(self):
        self.persist(speed=6)
        self.audit.side_effect = SequenceFieldAuditError('unsaved field')
        for _ in range(2):
            held = self.poll()
            self.assertTrue(held.control_recoverable)
            self.assertFalse(held.control_available)
        self.audit.side_effect = None
        with patch('windows_ui_adapter.sequence_persistence.capture', side_effect=sequence_persistence.PersistenceUnavailable('partial write')):
            self.assertTrue(self.poll().control_recoverable)
        self.assertTrue(self.poll().control_recoverable)
        self.assertTrue(self.poll().control_available)
        self.assertEqual(0, self.state['saves'])

    def test_full_real_field_audit_blocks_unsaved_then_accepts_stable_match(self):
        from sequence_field_audit import assert_fields_match_persisted, field_bindings
        bindings = field_bindings('M1')
        fields, entries = {}, []
        for field, (scope, name, kind) in bindings.items():
            value = '62' if field == 'textBoxHeatingOutput' else ('False' if kind == 'toggle' else '0')
            entries.append({'Scope': scope, 'Name': name, 'Type': 'BoolParameter' if kind == 'toggle' else 'DoubleParameter', 'Value': value})
            fields[field] = 'toggle:0' if kind == 'toggle' else value
        self.path.write_text(json.dumps(entries), encoding='utf-8')
        self.audit.side_effect = assert_fields_match_persisted
        original = self.adapter._runner
        def runner(request, stop):
            response = original(request, stop)
            if request['operation'] == 'recover_control':
                response['fields'] = dict(fields)
                response['field_map'] = {field: {'hwnd': 100 + i, 'kind': kind} for i, (field, (_, _, kind)) in enumerate(bindings.items())}
            return response
        self.adapter._runner = runner
        fields['textBoxLayerThickness'] = '1'
        self.assertTrue(self.poll().control_recoverable)
        self.assertTrue(self.poll().control_recoverable)
        fields['textBoxLayerThickness'] = '0'
        self.assertTrue(self.poll().control_recoverable)
        self.assertTrue(self.poll().control_available)
        self.assertEqual(0, self.state['saves'])

    def test_candidate_a_b_a_and_recreated_map_need_two_matching_checks(self):
        self.persist(speed=6)
        self.assertTrue(self.poll().control_recoverable)
        self.recovery['version'] = 2
        self.assertTrue(self.poll().control_recoverable)
        self.recovery['version'] = 1
        self.assertTrue(self.poll().control_recoverable)
        self.assertTrue(self.poll().control_available)
        self.assertEqual(1, self.poll().control_epoch)
        self.assertEqual(0, self.state['saves'])

    def test_wrong_identity_and_unknown_native_error_are_terminal(self):
        for error in (TargetIdentityError('wrong identity'), AdapterError('unknown native failure')):
            with self.subTest(error=error):
                self.adapter.suspend_control('test hold')
                self.recovery['error'] = error
                with self.assertRaises(type(error)):
                    self.poll()
        self.recovery['error'] = None
        self.recovery['machine'] = 'S3'
        with self.assertRaises(TargetIdentityError):
            self.poll()

    def test_read_timeout_holds_and_stop_prevents_adoption(self):
        self.adapter.suspend_control('test hold')
        self.recovery['error'] = UiReadTimeout('timeout')
        self.assertTrue(self.poll().control_recoverable)
        self.recovery['error'] = None
        self.assertTrue(self.poll().control_recoverable)
        stop = threading.Event()
        self.recovery['hook'] = stop.set
        self.now[0] += 1.1
        self.adapter._last_read = 0
        with self.assertRaises(AdapterError):
            self.adapter.read_snapshot(stop)
        self.assertEqual(0, self.adapter._control_epoch)

    def test_recovery_poll_throttles_and_disconnect_prevents_adoption(self):
        self.adapter.suspend_control('hold')
        first = self.poll()
        self.assertTrue(first.control_recoverable)
        count = sum(c['operation'] == 'recover_control' for c in self.calls)
        self.assertTrue(self.poll(step=.3).control_recoverable)
        self.assertEqual(count, sum(c['operation'] == 'recover_control' for c in self.calls))
        self.recovery['hook'] = self.adapter.disconnect
        with self.assertRaises(AdapterError):
            self.poll(step=1.1)
        self.assertEqual(0, self.adapter._control_epoch)

    def test_preedit_suspension_is_recoverable_but_marker_is_ambiguous(self):
        for marker in (None, 'edit_marker', 'save_marker'):
            with self.subTest(marker=marker), tempfile.TemporaryDirectory() as d:
                adapter, state, calls, path, values, append, baseline, source, audit = test_windows_ui_adapter.AdapterTests.fixture(self, d)
                fresh = append()
                original = adapter._runner
                def runner(request, stop):
                    if request['operation'] == 'apply':
                        if marker:
                            Path(request[marker]).write_text('started')
                        raise ControlSuspended('settings changed')
                    return original(request, stop)
                adapter._runner = runner
                with self.assertRaises(AmbiguousWriteError if marker else ControlSuspended):
                    adapter.apply_ir_power(1, 67, fresh.observation_key, threading.Event())
                self.assertEqual(0, state['saves'])
                if marker:
                    with self.assertRaises(Exception):
                        adapter.apply_ir_power(1, 67, fresh.observation_key, threading.Event())
                else:
                    self.assertTrue(adapter.read_snapshot(threading.Event()).control_recoverable)

class RecoverySessionTests(unittest.TestCase):
    def test_hold_resumes_only_after_baseline_and_fresh_different_layer(self):
        adapter = Mock()
        def make(key, layer, **kw):
            defaults = {'control_available': True, 'observation_is_new': True}
            defaults.update(kw)
            return replace(test_session.snapshot(key, layer), **defaults)
        frames = [make('old', 1, observation_is_new=False), make('hold', 2, control_available=False, control_recoverable=True), make('recovered', 2, control_epoch=1), make('recovered', 2, control_epoch=1), make('same-layer-correction', 2, control_epoch=1), make('fresh', 3, control_epoch=1)]
        adapter.read_snapshot.side_effect = frames
        stop, reports = threading.Event(), []
        controller = Mock()
        controller.process.return_value = type('Result', (), {'power_percent':55, 'reason':'test'})()
        def emit(kind, payload):
            if kind == 'snapshot':
                reports.append(payload)
                if len(reports) == len(frames):
                    stop.set()
        with patch('session.LayerAdaptiveController', return_value=controller):
            run_ui_session(SessionOptions(test_session.TARGET, mode='control'), stop, emit, lambda: adapter)
        self.assertEqual(len(frames), len(reports))
        for report in reports[1:5]:
            self.assertEqual({}, report['proposed'])
        self.assertEqual('fresh', adapter.apply_ir_power.call_args.args[2])
        self.assertEqual(1, adapter.apply_ir_power.call_count)

    def test_simultaneous_image_and_control_handoff_both_wait_for_fresh_layer(self):
        import test_slice_handoff as handoff
        options = SessionOptions(test_session.TARGET, mode='control', workbook_spec=handoff.SPEC, slice_dataset=handoff.A)
        adapter = Mock()
        def frame(key, layer, **kwargs):
            fields = {'control_available': True, 'observation_is_new': True}
            fields.update(kwargs)
            return replace(test_session.snapshot(key, layer), **fields)
        frames = [frame('old', 1, observation_is_new=False), frame('hold', 2, control_available=False, control_recoverable=True),
                  frame('new-epoch', 2, control_epoch=1), frame('same-layer', 2, control_epoch=1),
                  frame('fresh', 3, control_epoch=1)]
        adapter.read_snapshot.side_effect = frames
        stop = threading.Event()
        excel = Mock()
        excel.process.return_value = type('Result', (), {'power_percent':55, 'reason':'test'})()
        reports = []
        def emit(kind, payload):
            if kind == 'snapshot':
                reports.append(payload)
                if len(reports) == 2:
                    options._slice_dataset_setting.request(handoff.B)
                if len(reports) == len(frames):
                    stop.set()
        with patch('session.WorkbookController', return_value=excel):
            run_ui_session(options, stop, emit, lambda: adapter)
        for row in reports[1:4]:
            self.assertEqual({}, row['proposed'])
        adapter.apply_ir_power.assert_called_once()
        self.assertEqual('fresh', adapter.apply_ir_power.call_args.args[2])

    def test_each_heater_save_checks_image_epoch_and_stops_after_handoff(self):
        import test_slice_handoff as handoff
        options = SessionOptions(test_session.TARGET, mode='control', workbook_spec=handoff.SPEC, slice_dataset=handoff.A)
        adapter, excel, stop = Mock(), Mock(), threading.Event()
        excel.process.return_value = type('Result', (), {'power_percent':55, 'reason':'test'})()
        adapter.read_snapshot.return_value = replace(test_session.snapshot('fresh', 1), observation_is_new=True,
            control_available=True, coupled_output=False, control_epoch=1)
        def apply(heater, power, key, stop_event):
            self.assertEqual(1, heater)
            options._slice_dataset_setting.request(handoff.B)
        adapter.apply_ir_power.side_effect = apply
        reports = []
        def emit(kind, value):
            if kind == 'snapshot':
                reports.append(value)
                stop.set()
        with patch('session.WorkbookController', return_value=excel):
            run_ui_session(options, stop, emit, lambda: adapter)
        adapter.apply_ir_power.assert_called_once()
        self.assertEqual({1: 55}, reports[0]['applied'])
        self.assertEqual({}, reports[0]['proposed'])

    def test_cached_different_layer_does_not_bypass_recovery_barrier(self):
        adapter, stop, reports = Mock(), threading.Event(), []
        def frame(key, layer, **kwargs):
            values = {'control_available':True, 'observation_is_new':True}
            values.update(kwargs)
            return replace(test_session.snapshot(key, layer), **values)
        frames = [frame('old', 1, observation_is_new=False),
                  frame('hold', 2, control_available=False, control_recoverable=True),
                  frame('base', 2, control_epoch=1), frame('cached', 3, control_epoch=1, observation_is_new=False),
                  frame('rewrite', 3, control_epoch=1), frame('fresh', 4, control_epoch=1)]
        adapter.read_snapshot.side_effect = frames
        controller = Mock()
        controller.process.return_value = type('Result', (), {'power_percent':55, 'reason':'test'})()
        def emit(kind, payload):
            if kind == 'snapshot':
                reports.append(payload)
                if len(reports) == len(frames):
                    stop.set()
        with patch('session.LayerAdaptiveController', return_value=controller):
            run_ui_session(SessionOptions(test_session.TARGET, mode='control'), stop, emit, lambda: adapter)
        self.assertTrue(all(not report['proposed'] for report in reports[:-1]))
        adapter.apply_ir_power.assert_called_once()
        self.assertEqual('fresh', adapter.apply_ir_power.call_args.args[2])

    def test_second_heater_rechecks_control_epoch_before_save(self):
        adapter, stop, reports = Mock(), threading.Event(), []
        controller = Mock()
        controller.process.return_value = type('Result', (), {'power_percent':55, 'reason':'test'})()
        adapter.read_snapshot.return_value = replace(test_session.snapshot('fresh', 1), observation_is_new=True,
            control_available=True, coupled_output=False, control_epoch=0)
        adapter.validate_control_epoch.side_effect = [None, ControlSuspended('settings changed')]
        def emit(kind, payload):
            if kind == 'snapshot':
                reports.append(payload)
                stop.set()
        with patch('session.LayerAdaptiveController', return_value=controller):
            run_ui_session(SessionOptions(test_session.TARGET, mode='control'), stop, emit, lambda: adapter)
        self.assertEqual(2, adapter.validate_control_epoch.call_count)
        adapter.apply_ir_power.assert_called_once()
        self.assertEqual(1, adapter.apply_ir_power.call_args.args[0])
        self.assertEqual({1:55}, reports[0]['applied'])
        self.assertEqual({}, reports[0]['proposed'])
