"""New jobs cannot inherit manual image flags or pending requests."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from session import SessionOptions, SliceDatasetSetting
from standalone_ir import ThermalFeedbackWindow
import test_slice_handoff as handoff
from test_slice_handoff import A, B, SPEC, TARGET

class ImageJobScopeTests(unittest.TestCase):
    def test_first_proven_stale_run_binds_scope_before_fresh_new_run(self):
        h=handoff.HandoffTests()
        frames=[h.frame('a',0,False,run_key='A',observation_fresh=False),
                h.frame('b',1,run_key='B',observation_fresh=True),
                h.frame('c',2,run_key='B',observation_fresh=True)]
        excel=h.excel()
        _,reports,events=h.run_case(frames,SessionOptions(TARGET,mode='preview',workbook_spec=SPEC,slice_dataset=A),excel)
        self.assertTrue(reports[0]['image_authorized'])
        self.assertFalse(reports[1]['image_authorized'])
        self.assertFalse(reports[2]['image_authorized'])
        self.assertIsNone(reports[2]['slice_features'])
        for call in excel.process.call_args_list:self.assertIsNone(call.kwargs.get('slice_features'))
    def test_old_job_request_is_retired_without_permanent_image_hold(self):
        h=handoff.HandoffTests();options=SessionOptions(TARGET,mode='preview',workbook_spec=SPEC,slice_dataset=A)
        frames=[h.frame(str(i),i,run_key='A' if i==0 else 'B',observation_fresh=True) for i in range(5)]
        def hook(n,row):
            if n==1:options._slice_dataset_setting.request(B,force=True,run_key='A')
        excel=h.excel();_,reports,events=h.run_case(frames,options,excel,hook)
        self.assertIsNone(options._slice_dataset_setting.active()[1])
        self.assertTrue(all(r['slice_features'] is None for r in reports[1:]))
        self.assertTrue(any('이전 작업의 이미지' in str(v) for k,v in events if k=='log'))
        self.assertTrue(excel.process.call_args_list)
        for call in excel.process.call_args_list:self.assertIsNone(call.kwargs.get('slice_features'))
    def test_same_fingerprint_can_be_reconfirmed_for_exact_current_job(self):
        setting=SliceDatasetSetting(A)
        epoch=setting.request(A,force=True,run_key='B')
        self.assertFalse(setting.activate(epoch,'A'))
        self.assertFalse(setting.activate(epoch,None))
        self.assertTrue(setting.activate(epoch,'B'))
    def test_unknown_run_reconfirmation_is_rejected_by_ui(self):
        callback=Mock()
        window=SimpleNamespace(closing=False,current_run_key=None,
            controller=SimpleNamespace(generation=3,thread=object(),state='connected',update_slice_dataset=callback))
        ThermalFeedbackWindow._publish_slice_dataset(window,A,(3,None))
        callback.assert_not_called()
    def test_old_job_analysis_completion_is_not_published_for_new_job(self):
        callback=Mock()
        window=SimpleNamespace(closing=False,current_run_key='B',
            controller=SimpleNamespace(generation=3,thread=object(),state='connected',update_slice_dataset=callback))
        ThermalFeedbackWindow._publish_slice_dataset(window,A,(3,'A'))
        callback.assert_not_called()

if __name__=='__main__':unittest.main()
