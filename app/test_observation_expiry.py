"""Known pre-edit time budget veto is consumed; completed actual inputs survive."""
from dataclasses import replace
from pathlib import Path
import tempfile,threading,unittest
from unittest.mock import patch
from adapter_contracts import ObservationExpired,AmbiguousWriteError,ObservationSuspended
from windows_ui_adapter import WindowsUiAdapter
import test_windows_ui_adapter as fixtures
import test_csv_observation_wait as observation

class ExpiryAdapterTests(unittest.TestCase):
    def test_deadline_response_is_distinct_and_only_safe_before_markers(self):
        for marker in (None,'edit_marker','save_marker'):
            with self.subTest(marker=marker),tempfile.TemporaryDirectory() as d:
                raw,state,calls,_,_,append,_,_,_=fixtures.AdapterTests().fixture(d)
                snap=append();runner=raw._runner
                def request(data,event):
                    if data['operation']=='apply':
                        if marker:Path(data[marker]).write_text('started')
                        return dict(success=False,error_kind='deadline_wait',error='expired before operation')
                    return runner(data,event)
                raw._runner=request
                with self.assertRaises(AmbiguousWriteError if marker else ObservationExpired):
                    raw.apply_ir_power(1,67,snap.observation_key,threading.Event())
                self.assertEqual(0,state['saves'])
                self.assertEqual(bool(marker),bool(raw._control_fault))
                self.assertIsNone(raw._control_hold_reason)
    def test_old_finished_time_remains_valid_but_future_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            raw,_,_,_,_,append,_,_,_=fixtures.AdapterTests().fixture(d)
            snap=append();source=raw._csv.read();finish=source.sample.finished.timestamp()
            with patch('windows_ui_adapter.time.time',return_value=finish+6):
                self.assertIsNotNone(raw._verify_csv_observation(snap.observation_key))
            with patch('windows_ui_adapter.time.time',return_value=finish-3):
                with self.assertRaises(ObservationSuspended) as e:raw._verify_csv_observation(snap.observation_key)
                self.assertNotIsInstance(e.exception,ObservationExpired)
    def test_snapshot_elapsed_does_not_discard_valid_current_layer(self):
        with tempfile.TemporaryDirectory() as d:
            raw,state,_,_,_,append,_,_,_=fixtures.AdapterTests().fixture(d)
            snap=append();raw._last_read-=4
            raw.apply_ir_power(1,67,snap.observation_key,threading.Event())
            self.assertEqual(1,state['saves'])
            self.assertIsNone(raw._control_fault)
            self.assertIsNone(raw._control_hold_reason)

class ExpirySessionTests(unittest.TestCase):
    def test_completed_history_and_next_fresh_contiguous_layer_survive(self):
        for algorithm in ('basic','excel'):
            with self.subTest(algorithm=algorithm):
                h=observation.ObservationSessionTests()
                frames=[h.frame('baseline',254,observation_is_new=False),h.frame('expired',255),
                        h.frame('expired',255),h.frame('next',256)]
                adapter,controller,reports=h.run_frames(frames,errors=[ObservationExpired('deadline'),None],algorithm=algorithm)
                self.assertEqual(['expired','next'],[x.args[2] for x in adapter.apply_ir_power.call_args_list])
                self.assertEqual([{}, {}, {}, {1:55}],[x['applied'] for x in reports])
                self.assertEqual([256,257],[x.args[0] for x in controller.process.call_args_list])
                controller.reset.assert_not_called()
                self.assertFalse(reports[1]['proposed'])
                adapter.suspend_control.assert_not_called()
    def test_confirmed_first_heater_save_survives_second_heater_deadline(self):
        h=observation.ObservationSessionTests()
        frames=[h.frame('baseline',254,observation_is_new=False,coupled_output=False),
                h.frame('expired',255,coupled_output=False)]
        adapter,controller,reports=h.run_frames(frames,errors=[None,ObservationExpired('deadline')])
        self.assertEqual({1:55},reports[-1]['applied'])
        self.assertFalse(reports[-1]['proposed'])
        controller.reset.assert_not_called()

class ExpiryRetentionGuardTests(unittest.TestCase):
    def test_pending_unconsumed_and_context_changes_do_not_preserve(self):
        from types import SimpleNamespace
        from excel_calculation import ExcelCalculation
        from test_excel_session_async import SPEC
        from test_session import snapshot
        calc=ExcelCalculation(SPEC,65,100,threading.Event())
        snap=snapshot('accepted',2,run_key='A',csv_generation='file-A',control_epoch=3)
        powers={1:50};heaters=(1,);target=(0,65);cap=(0,80);image=7
        calc.group=SimpleNamespace(state='ready')
        calc._context=calc._context_for(snap,powers,heaters,target,image,cap)
        calc._accepted_identity=calc._identity(snap);calc.accepted_stamp=(target,image);calc.accepted_cap_stamp=cap
        def allowed(frame=snap):return calc.can_retain_after_expiry(frame,powers,heaters,target,image,cap)
        self.assertTrue(allowed())
        for name,value in (('pending',True),('_completed',{}),('_resetting',True),('_recovery','starting'),('accepted_cap_stamp',(1,70))):
            old=getattr(calc,name);setattr(calc,name,value)
            self.assertFalse(allowed(),name);setattr(calc,name,old)
        for frame in (replace(snap,run_key='B'),replace(snap,csv_generation='file-B'),replace(snap,control_epoch=4)):
            self.assertFalse(allowed(frame))
    def test_epoch_change_during_deadline_veto_resets_history(self):
        from unittest.mock import Mock
        from adapter_contracts import UiAdapter,ControlSuspended
        from session import SessionOptions,run_ui_session
        from test_session import TARGET
        h=observation.ObservationSessionTests()
        frames=[h.frame('baseline',0,observation_is_new=False),h.frame('expired',1)]
        adapter=Mock(spec=UiAdapter);adapter.read_snapshot.side_effect=frames
        adapter.apply_ir_power.side_effect=ObservationExpired('expired')
        adapter.validate_control_epoch.side_effect=[None,ControlSuspended('changed')]
        controller=Mock();controller.process.return_value=type('Result',(),{'power_percent':55,'reason':'test'})()
        reports=[];stop=threading.Event()
        def emit(kind,value):
            if kind=='snapshot':
                reports.append(value)
                if len(reports)==2:stop.set()
        with patch('session.LayerAdaptiveController',return_value=controller):
            run_ui_session(SessionOptions(TARGET,mode='control'),stop,emit,lambda:adapter)
        self.assertTrue(controller.reset.called)
        self.assertFalse(reports[-1]['applied'])

if __name__=='__main__':unittest.main()
