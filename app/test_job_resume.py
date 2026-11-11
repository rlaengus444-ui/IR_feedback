"""Offline restart admission, chronology, endpoint and write-fence regressions."""
import unittest
from dataclasses import replace
from adapter_contracts import ControlSuspended, UiSnapshot
from resilient_adapter import ResilientUiAdapter
from test_resilient_adapter import Raw, Reader, TARGET, NEW

class JobResumeTests(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        self.raws = []
        def factory(source_factory):
            raw = Raw(source_factory)
            raw.ambiguous = False
            raw.holds = []
            raw.suspend_control = raw.holds.append
            self.raws.append(raw)
            return raw
        self.candidates = [NEW]
        self.adapter = ResilientUiAdapter(factory, lambda:self.candidates, Reader, lambda:self.now)
        self.adapter.connect(TARGET, None)
        self.adapter.prepare_mode('control', None)
        self.read('A',1,1000)
        self.assertTrue(self.read('A',2,1000).control_available)
    def read(self, job, layer, started, **kw):
        snap = UiSnapshot(target=TARGET, actual_target=self.adapter.current_target,
            run_key=job, job_auto_started=started, job_first_started=started+1,
            job_first_layer=0, job_current_layer=layer+1, layer_index=layer,
            job_start_offset=int(started), job_log_identity='owned-log',
            machine_state='AUTO', observation_key=job+str(layer), observation_is_new=True,
            observation_fresh=True, control_available=True)
        self.raws[-1].snapshot=replace(snap, **kw)
        return self.adapter.read_snapshot(None)
    def adopt(self, job='B', started=2000):
        self.assertFalse(self.read(job,0,started).control_available)
        self.assertFalse(self.read(job,0,started).control_available)
        self.now += 1.1
        self.assertFalse(self.read(job,0,started).control_available) # adoption, no pre-audit replay
        self.assertEqual(job,self.adapter._pinned_job)
        self.assertFalse(self.read(job,0,started).control_available) # baseline
        self.assertFalse(self.read(job,0,started,observation_key='same-layer-new-key').control_available)
        self.assertTrue(self.read(job,1,started).control_available)
    def test_stop_start_new_job_resumes_automatically(self):
        original_epoch=self.adapter._epoch
        self.read('A',2,1000,machine_state='IDLE')
        self.adopt()
        self.assertGreater(self.adapter._epoch,original_epoch)
        self.assertIn('A',self.adapter._retired_jobs)
        self.adapter.apply_ir_power(1,55,'B1',None)
        self.assertEqual(1,self.raws[-1].calls)
    def test_fresh_new_run_does_not_need_observed_stop(self): self.adopt()
    def test_initial_idle_does_not_create_an_unrecoverable_handoff(self):
        self.adapter._pinned_job=None;self.adapter._pinned_proof=None
        self.adapter._job_highwater=None;self.adapter._was_ready=False
        self.assertFalse(self.read('A',0,1000,machine_state='IDLE').control_available)
        self.assertFalse(self.adapter._job_handoff)
        self.read('A',0,1000)
        self.assertTrue(self.read('A',1,1000).control_available)
    def test_initial_stale_proven_run_can_transition_to_fresh_new_job(self):
        self.adapter._pinned_job=None;self.adapter._pinned_proof=None
        self.adapter._job_highwater=None;self.adapter._was_ready=False
        self.read('A',0,1000,observation_fresh=False,observation_key=None)
        self.assertEqual(1000,self.adapter._pinned_proof[3])
        self.adopt()
    def test_old_run_flicker_during_candidate_never_rearms(self):
        self.read('B',0,2000)
        self.assertFalse(self.read('A',3,1000).control_available)
        self.assertTrue(self.adapter._job_handoff)
        self.adopt()
    def test_retired_run_cannot_save_after_adoption(self):
        self.adopt()
        self.assertFalse(self.read('A',9,1000).control_available)
        with self.assertRaises(ControlSuspended):self.adapter.apply_ir_power(1,60,'A9',None)
        self.assertFalse(self.read('B',2,2000).control_available)
        self.assertTrue(self.read('B',3,2000).control_available)
    def test_older_equal_or_missing_start_proofs_do_not_adopt(self):
        for stamp in (900,1000,None):
            self.read('C',0,2000,job_auto_started=stamp)
            self.now+=2
            self.assertFalse(self.read('C',1,2000,job_auto_started=stamp).control_available)
            self.assertEqual('A',self.adapter._pinned_job)
    def test_same_timestamp_different_job_cannot_supersede_candidate(self):
        self.read('B',0,2000)
        for _ in range(4):
            self.now+=2
            self.assertFalse(self.read('C',1,2000).control_available)
        self.assertEqual('A',self.adapter._pinned_job)
        self.adopt()
    def test_same_second_restart_uses_later_start_occurrence(self):
        def read(layer):return self.read('B',layer,1000,job_start_offset=2000)
        self.assertFalse(read(0).control_available)
        self.assertFalse(read(0).control_available)
        self.now+=1.1
        self.assertFalse(read(0).control_available)
        self.assertEqual('B',self.adapter._pinned_job)
        self.assertFalse(read(0).control_available)
        self.assertTrue(read(1).control_available)
    def test_higher_candidate_prevents_regression_to_abandoned_candidate(self):
        self.read('B',0,2000)
        self.read('C',0,3000)
        for _ in range(4):
            self.now+=2
            self.assertFalse(self.read('B',1,2000).control_available)
        self.assertIn('B',self.adapter._retired_jobs)
        self.adopt('C',3000)
    def test_invalid_or_idle_or_settings_hold_interrupts_confirmation(self):
        for kwargs in ({'measurement_valid':False},{'observation_fresh':False},
                       {'observation_key':None},{'machine_state':'IDLE'},
                       {'job_current_layer':77},{'control_available':False}):
            self.read('B',0,2000)
            self.read('B',0,2000)
            self.now+=2
            self.assertFalse(self.read('B',0,2000,**kwargs).control_available)
            self.assertEqual('A',self.adapter._pinned_job)
            self.assertFalse(self.read('B',0,2000).control_available)
            self.assertEqual('A',self.adapter._pinned_job)
        self.adopt()
    def test_candidate_cannot_span_pid_change(self):
        self.read('B',0,2000);self.read('B',0,2000)
        self.now+=2
        self.adapter.current_target=NEW
        self.assertFalse(self.read('B',0,2000).control_available)
        self.assertEqual('A',self.adapter._pinned_job)
        self.assertNotIn('B',self.adapter._retired_jobs)
        self.adopt()
    def test_same_job_pid_rebind_retains_watermarks_and_reaudits(self):
        self.adapter.apply_ir_power(1,55,'A2',None)
        self.adapter._outage('process exited')
        self.now=3
        self.adapter._reconnect(None)
        self.assertEqual(NEW,self.adapter.current_target)
        self.assertFalse(self.read('A',2,1000).control_available)
        self.assertEqual(['monitor','control'],self.raws[-1].modes)
        self.assertTrue(any('재연결' in x for x in self.raws[-1].holds))
        self.assertFalse(self.read('A',1,1000).control_available)
        self.assertTrue(self.read('A',3,1000).control_available)
        self.assertEqual(2,self.adapter._watermark[('A',1)])
    def test_sticky_save_barrier_survives_new_job_and_new_pid(self):
        self.adapter.freeze_control('Save uncertain')
        self.read('B',0,2000);self.now+=5
        self.assertFalse(self.read('B',1,2000).control_available)
        self.adapter._outage('process exited');self.now+=3;self.adapter._reconnect(None)
        self.assertFalse(self.read('B',2,2000).control_available)
        self.assertEqual('Save uncertain',self.adapter._sticky)
        self.assertNotIn('control',self.raws[-1].modes)
    def test_non_unique_or_wrong_executable_never_rebinds(self):
        for candidates in ([NEW,replace(NEW,pid=99)], [replace(NEW,image_path=r'C:\other\BJT.exe')],
                           [replace(NEW,machine='S3')], []):
            self.adapter._outage('offline');self.now+=100;self.candidates=candidates
            self.adapter._reconnect(None)
            self.assertIsNone(self.adapter._raw)
    def test_read_only_bootstrap_hold_recovers_without_permanent_barrier(self):
        self.adapter._prepared_mode='monitor'
        self.adapter._offline=lambda:UiSnapshot(target=TARGET)
        self.raws[-1].read_native_state=lambda event:(_ for _ in ()).throw(ControlSuspended('state label is being recreated'))
        self.assertFalse(self.read('A',3,1000).control_available)
        self.assertFalse(self.adapter._sticky)
        self.raws[-1].read_native_state=lambda event:'AUTO'
        self.assertFalse(self.read('A',4,1000).control_available)
        self.assertTrue(self.read('A',5,1000).control_available)

if __name__=='__main__':unittest.main()
