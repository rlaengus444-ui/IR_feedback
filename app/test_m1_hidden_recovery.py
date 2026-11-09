"""Offline proof propagation and fresh-layer recovery barriers; no native UI."""
from dataclasses import replace
import threading
import unittest
from unittest.mock import Mock, patch
from session import SessionOptions, run_ui_session
import test_live_settings as live
import test_session

class HiddenRecoveryAdapterTests(unittest.TestCase):
    setUp = live.RecoveryAdapterTests.setUp
    poll = live.RecoveryAdapterTests.poll
    persist = live.RecoveryAdapterTests.persist
    def test_scope_proof_is_carried_and_change_requires_two_new_checks(self):
        proof = {'scope_hwnd': 10, 'scope_class': 'Panel', 'scope_parent': 8,
                 'scope_thread': 3, 'scope_control_id': 7,
                 'ir_hwnd': 11, 'ir_class': 'Edit', 'ir_id': 'textBoxHeatingOutput',
                 'save_hwnd': 12, 'save_class': 'Button', 'save_id': 'btnSave',
                 'machine_hwnd': 13, 'machine_class': 'Static', 'machine_id': 'labelMachineName',
                 'state_hwnd': 99, 'state_class': 'Static', 'state_id': 'labelTaskStatus'}
        self.adapter._native_handles['scope_proof'] = dict(proof)
        original = self.adapter._runner
        proof_version = [dict(proof)]
        def runner(request, stop):
            response = original(request, stop)
            if request['operation'] == 'recover_control':
                self.assertEqual(proof, request['handles']['scope_proof'])
                response['handles']['scope_proof'] = dict(proof_version[0])
            return response
        self.adapter._runner = runner
        self.persist(speed=6)
        self.assertTrue(self.poll().control_recoverable)
        proof_version[0]['scope_parent'] = 9
        self.assertTrue(self.poll().control_recoverable)
        proof_version[0]['scope_parent'] = 8
        self.assertTrue(self.poll().control_recoverable)
        result = self.poll()
        self.assertTrue(result.control_available)
        self.assertEqual(1, result.control_epoch)
        self.assertEqual(proof, self.adapter._native_handles['scope_proof'])
        self.assertEqual(0, self.state['saves'])

class HiddenRecoverySessionTests(unittest.TestCase):
    def test_held_cached_and_same_layer_rows_do_not_save(self):
        adapter = Mock()
        def frame(key, layer, **changes):
            values = {'control_available': True, 'observation_is_new': True, 'coupled_output': True}
            values.update(changes)
            return replace(test_session.snapshot(key, layer), **values)
        frames = [frame('initial', 4, observation_is_new=False),
                  frame('hidden', 5, control_available=False, control_recoverable=True),
                  frame('baseline', 5, control_epoch=1), frame('cached', 6, control_epoch=1, observation_is_new=False),
                  frame('corrected', 6, control_epoch=1), frame('new', 7, control_epoch=1)]
        adapter.read_snapshot.side_effect = frames
        stop, reports = threading.Event(), []
        controller = Mock()
        controller.process.return_value = type('Result', (), {'power_percent':55, 'reason':'test'})()
        def emit(kind, payload):
            if kind == 'snapshot':
                reports.append(payload)
                if len(reports) < len(frames): adapter.apply_ir_power.assert_not_called()
                else: stop.set()
        with patch('session.LayerAdaptiveController', return_value=controller):
            run_ui_session(SessionOptions(test_session.TARGET, mode='control'), stop, emit, lambda: adapter)
        adapter.apply_ir_power.assert_called_once()
        self.assertEqual('new', adapter.apply_ir_power.call_args.args[2])

class RestoreClassifierTests(unittest.TestCase):
    def test_retirement_stage_interval_and_message_fail_closed(self):
        import sys
        import os
        from pathlib import Path
        workspace = os.environ.get('GITHUB_WORKSPACE')
        tools_dir = Path(workspace) / 'release_tools' if workspace else Path(__file__).resolve().parents[1] / 'release_tools'
        self.assertTrue((tools_dir / 'windows_hidden_recovery_probe.py').is_file())
        sys.path.insert(0, str(tools_dir))
        self.addCleanup(sys.path.pop, 0)
        from windows_hidden_recovery_probe import normalize_restore_messages as classify
        events = ['2026-11-09T09:50:58.0000000Z|begin', '2026-11-09T09:50:59.0000000Z|end']
        message = '2026-11-09T09:50:58.5000000Z|21:fixture-restore|D|1'
        interval = {'transport':'candidate', 'begin_utc':'2026-11-09T09:50:57+00:00',
            'end_utc':'2026-11-09T09:50:57.9+00:00', 'helper_retired':True,
            'active_helper_absent':True, 'unretired_helper_absent':True,
            'helper_fence_unlocked':True, 'shared_fence_verified':True}
        self.assertEqual(['D','D'], classify([message,message], events, [interval]))
        for key in ('helper_retired','active_helper_absent','unretired_helper_absent',
                    'helper_fence_unlocked','shared_fence_verified'):
            for value in (False, None):
                with self.subTest(field=key, value=value):
                    self.assertIsNone(classify([message,message], events, [dict(interval, **{key:value})]))
            missing = dict(interval); del missing[key]
            self.assertIsNone(classify([message,message], events, [missing]))
        for invalid in (message[:-1]+'0', message.replace('58.500','59.500'),
            message.replace('21:fixture-restore','arbitrary:fixture-restore'),
            message.replace('21:fixture-restore','0:fixture-restore'),
            message.replace('|D|','|3D|'), message.replace('|D|','|114|')):
            with self.subTest(message=invalid):
                self.assertIsNone(classify([message,invalid], events, [interval]))
        overlap = dict(interval, begin_utc='2026-11-09T09:50:58.1+00:00', end_utc='2026-11-09T09:50:58.2+00:00')
        self.assertIsNone(classify([message,message], events, [overlap]))
        self.assertIsNone(classify([message,message], events[:1], [interval]))
        self.assertIsNone(classify([message,message], events, [dict(interval, transport='old')]))
