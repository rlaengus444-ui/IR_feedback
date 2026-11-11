"""Owned-helper completion evidence and autonomous higher-layer admission."""
import json
import threading
import unittest
from pathlib import Path
from dataclasses import replace
from unittest.mock import Mock, patch
from adapter_contracts import AmbiguousWriteError, SaveConfirmationPending
import test_live_settings as live
import test_session
from session import SessionOptions, run_ui_session


class SaveReconciliationTests(unittest.TestCase):
    def setUp(self):
        live.RecoveryAdapterTests.setUp(self)
        # Fake helpers have no UI recipient or OS process; test isolation only.
        self.addCleanup(lambda: setattr(type(self.adapter), '_save_barrier', None))
    poll = live.RecoveryAdapterTests.poll
    persist = live.RecoveryAdapterTests.persist

    def begin(self):
        snap = self.append()
        self.helper = Mock()
        self.helper.poll.return_value = None
        self.helper.returncode = 0
        self.helper.communicate.return_value = (json.dumps(dict(success=True,save_invoked=True,setting='67')), '')
        self.delegate = self.adapter._runner
        def runner(request, stop):
            if request['operation'] == 'apply':
                self.calls.append(request)
                Path(request['edit_marker']).write_text('edit')
                Path(request['save_marker']).write_text('save')
                self.state['saves'] += 1
                self.request = request
                self.adapter._active_helper = self.helper
                raise SaveConfirmationPending('Owned synchronous Save still running')
            return self.delegate(request,stop)
        self.adapter._runner = runner
        with self.assertRaises(SaveConfirmationPending):
            self.adapter.apply_ir_power(1,67,snap.observation_key,threading.Event())
        self.assertIsNone(self.adapter._control_fault)
        self.assertEqual(1,self.state['saves'])
        self.old_key = snap.observation_key
        return snap

    def complete(self, **changes):
        r=self.request
        receipt=dict(transaction_id=r['transaction_id'],pid=r['pid'],hwnd=r['hwnd'],power=r['power'],completed=True)
        receipt.update(changes)
        Path(r['dispatch_receipt']).write_text(json.dumps(receipt))
        self.helper.poll.return_value=0
        self.persist(power=67)

    def test_pending_csv_continues_without_native_requests_or_second_save(self):
        self.begin();count=len(self.calls)
        snap=self.append(index=2)
        self.assertEqual(2,snap.layer_index)
        self.assertTrue(snap.control_recoverable)
        self.assertFalse(snap.control_available)
        self.assertEqual(count,len(self.calls))
        self.assertTrue(Path(self.request['save_marker']).exists())
        self.assertEqual(1,self.state['saves'])

    def test_receipt_while_helper_alive_never_rearms(self):
        self.begin();self.complete();self.helper.poll.return_value=None
        for _ in range(3):self.assertFalse(self.poll().control_available)
        self.assertEqual(1,self.state['saves'])

    def test_completed_save_two_audits_then_distinct_higher_row_uses_actual_power(self):
        self.begin();self.complete()
        self.assertFalse(self.poll().control_available)
        self.assertTrue(self.poll().control_available)
        stamp=self.adapter._reconciled_save
        self.assertEqual((self.adapter._run_key,1),stamp[1:])
        self.assertEqual(1,self.state['saves'])
        self.adapter._runner=self.delegate
        snap=self.append(index=2)
        self.assertEqual(67,snap.heater1_setting_percent)
        self.adapter.apply_ir_power(1,70,snap.observation_key,threading.Event())
        self.assertEqual(2,self.state['saves'])
        self.assertNotEqual(self.old_key,snap.observation_key)

    def test_tampered_nonce_or_identity_does_not_rearm(self):
        self.begin();self.complete(transaction_id='wrong')
        with self.assertRaises(AmbiguousWriteError):self.poll()
        self.assertEqual(1,self.state['saves'])

    def test_missing_receipt_after_helper_exit_does_not_rearm(self):
        self.begin();self.helper.poll.return_value=0
        with self.assertRaises(AmbiguousWriteError):self.poll()

    def test_corrupt_helper_protocol_never_rearms(self):
        self.begin();self.complete();self.helper.communicate.return_value=('not-json','')
        with self.assertRaises(AmbiguousWriteError):self.poll()
        self.assertFalse(self.adapter._pending_save['completed'])

    def test_non_ir_change_or_mismatching_persistence_stays_observing(self):
        self.begin();self.complete();self.persist(speed=6)
        for _ in range(3):self.assertFalse(self.poll().control_available)
        self.assertEqual(1,self.state['saves'])

    def test_user_edit_audit_keeps_hold_until_resolved(self):
        from sequence_field_audit import SequenceFieldAuditError
        self.begin();self.complete()
        self.audit.side_effect=SequenceFieldAuditError('operator editing')
        for _ in range(2):self.assertFalse(self.poll().control_available)
        self.audit.side_effect=None
        self.assertFalse(self.poll().control_available)
        self.assertTrue(self.poll().control_available)
        self.assertEqual(1,self.state['saves'])

    def test_new_adapter_cannot_bypass_unresolved_owned_transaction(self):
        from windows_ui_adapter import WindowsUiAdapter
        self.begin();self.adapter.disconnect()
        called=Mock()
        replacement=WindowsUiAdapter(runner=called)
        with self.assertRaises(AmbiguousWriteError):
            replacement._request({'operation':'prepare_control'},None)
        called.assert_not_called()

    def test_boolean_receipt_alias_is_rejected(self):
        self.begin();self.complete(completed=1)
        with self.assertRaises(AmbiguousWriteError):self.poll()

    def test_non_ir_persistence_fault_never_converts_to_pending(self):
        import sequence_persistence
        snap=self.append()
        delegate=self.adapter._runner
        def runner(request,stop):
            result=delegate(request,stop)
            if request['operation']=='apply':
                Path(request['dispatch_receipt']).write_text('{}')
            return result
        self.adapter._runner=runner
        with patch('windows_ui_adapter.sequence_persistence.verify_after',
                side_effect=sequence_persistence.PersistenceMismatch('non IR changed')):
            with self.assertRaises(AmbiguousWriteError):
                self.adapter.apply_ir_power(1,67,snap.observation_key,threading.Event())
        self.assertIsNone(self.adapter._pending_save)


class SaveResumeSessionTests(unittest.TestCase):
    def test_first_fresh_higher_layer_enters_reset_engine_without_replaying_failed_row(self):
        adapter=Mock()
        def frame(key,layer,**kw):
            return test_session.snapshot(key,layer,run_key='job',observation_is_new=True,
                observation_fresh=True,control_available=True,**kw)
        frames=[replace(frame('a',1),observation_is_new=False),frame('b',2),
            replace(frame('b',2),control_available=False,control_recoverable=True),
            replace(frame('c',3,control_epoch=1,save_reconciliation=('transaction','job',2)),heater1_setting_percent=55),
            replace(frame('c',3,control_epoch=1,save_reconciliation=('transaction','job',2)),heater1_setting_percent=60),
            replace(frame('d',4,control_epoch=1,save_reconciliation=('transaction','job',2)),heater1_setting_percent=60)]
        adapter.read_snapshot.side_effect=frames
        attempts=[]
        def apply(heater,power,key,event):
            attempts.append(key)
            if key=='b':raise SaveConfirmationPending('waiting')
        adapter.apply_ir_power.side_effect=apply
        controller=Mock();controller.process.side_effect=lambda layer,temp,power,**kw:type('R',(),{'power_percent':power+5,'reason':'test'})()
        stop=threading.Event();rows=[]
        def emit(kind,payload):
            if kind=='snapshot':
                rows.append(payload)
                if len(rows)==len(frames):stop.set()
        with patch('session.LayerAdaptiveController',return_value=controller):
            run_ui_session(SessionOptions(test_session.TARGET,mode='control'),stop,emit,lambda:adapter)
        self.assertEqual(['b','c','d'],attempts)
        self.assertEqual({},rows[1]['applied'])
        self.assertEqual({1:60},rows[3]['applied'])
        self.assertEqual(3,controller.process.call_count)
