"""Offline image handoff races; all adapters and Excel engines are synthetic."""
import hashlib
import threading
import unittest
from unittest.mock import Mock, patch

from adapter_contracts import UiAdapter
from excel_workbook import WorkbookSpec
from layer_algorithm import LayerRecommendation
from session import SessionController, SessionOptions, SliceDatasetSetting, WorkerEvent, run_ui_session
from slice_data import SliceConfig, SliceDataset, SliceRecord
from test_session import TARGET, snapshot, DeferredThread


SPEC = WorkbookSpec('fixture.xlsx', '', 'B13', 'C13', 'D13', 'B5', 'A13', 'BL13')


class FastStop(threading.Event):
    def wait(self, timeout=None):
        return self.is_set()


def dataset(ink):
    sha = hashlib.sha256(str(ink).encode()).hexdigest()
    return SliceDataset(SliceConfig(1, 25.4, 25.4), tuple(
        SliceRecord(i, f'{i:03}.png', ink, ink, ink, 4, 20, 20, sha,
                    i if not ink else 0) for i in range(1, 30)), sha)


A, B, C = dataset(0), dataset(50), dataset(100)


class HandoffTests(unittest.TestCase):
    def run_case(self, frames, options, excel=None, hook=None, read_hook=None, apply_hook=None):
        adapter = Mock(spec=UiAdapter)
        stop = FastStop()
        reports, events = [], []
        iterator = iter(frames)
        def read(_):
            value = next(iterator)
            if read_hook:
                read_hook(value)
            return value
        adapter.read_snapshot.side_effect = read
        if apply_hook:
            adapter.apply_ir_power.side_effect = apply_hook
        def emit(kind, value):
            events.append((kind, value))
            if kind == 'snapshot':
                reports.append(value)
                if hook:
                    hook(len(reports), value)
                if len(reports) == len(frames):
                    stop.set()
        if excel is None:
            run_ui_session(options, stop, emit, lambda: adapter)
        else:
            with patch('session.WorkbookController', return_value=excel):
                run_ui_session(options, stop, emit, lambda: adapter)
        return adapter, reports, events

    @staticmethod
    def frame(key, layer, fresh=True, **kwargs):
        return snapshot(key, layer, observation_is_new=fresh, control_available=True, **kwargs)

    @staticmethod
    def excel(power=55):
        value = Mock()
        value.process.return_value = LayerRecommendation(power, 'fixture', {})
        return value

    def test_setting_requires_complete_dataset_and_rejects_superseded_activation(self):
        setting = SliceDatasetSetting(A)
        self.assertTrue(setting.admit(0))
        with self.assertRaises(ValueError):
            setting.request(object())
        self.assertEqual(1, setting.request(B))
        self.assertEqual((0, A), setting.active())
        self.assertFalse(setting.admit(0))
        self.assertEqual(2, setting.request(C))
        self.assertFalse(setting.activate(1))
        self.assertTrue(setting.activate(2))
        self.assertTrue(setting.admit(2))

    def test_image_excel_resets_once_then_skips_cached_and_same_layer_rows(self):
        options = SessionOptions(TARGET, mode='preview', workbook_spec=SPEC, slice_dataset=A)
        excel = self.excel()
        frames = [self.frame('a', 0, False), self.frame('b', 1), self.frame('b', 1, False),
                  self.frame('b-edited', 1), self.frame('cached-c', 2, False), self.frame('d', 3)]
        def hook(n, row):
            if n == 2:
                options._slice_dataset_setting.request(B)
        adapter, reports, events = self.run_case(frames, options, excel, hook)
        self.assertEqual([2, 4], [c.args[0] for c in excel.process.call_args_list])
        self.assertEqual([0, 50], [c.kwargs['slice_features']['whole_coverage_pct'] for c in excel.process.call_args_list])
        excel.reset.assert_called_once()
        adapter.apply_ir_power.assert_not_called()
        self.assertEqual([0, 0, 1, 1, 1, 1], [r['slice_epoch'] for r in reports])
        self.assertEqual([{}, {}, {}], [r['proposed'] for r in reports[2:5]])
        self.assertEqual(1, sum(k == 'slice_activated' for k, _ in events))

    def test_identical_reanalysis_is_noop_and_preserves_excel_history(self):
        options=SessionOptions(TARGET,mode='preview',workbook_spec=SPEC,slice_dataset=A)
        same=SliceDataset(A.config,tuple(A.records),A.fingerprint)
        excel=self.excel()
        def hook(n,row):
            if n==2:self.assertEqual(0,options._slice_dataset_setting.request(same))
        frames=[self.frame('a',0,False),self.frame('b',1),self.frame('c',2)]
        _,reports,_=self.run_case(frames,options,excel,hook)
        excel.reset.assert_not_called()
        self.assertEqual(2,excel.process.call_count)
        self.assertEqual([0,0,0],[row['slice_epoch'] for row in reports])

    def test_return_to_active_after_different_pending_plan_keeps_new_epoch(self):
        setting=SliceDatasetSetting(A)
        self.assertEqual(1,setting.request(B))
        self.assertEqual(1,setting.request(dataset(50)))
        self.assertEqual(2,setting.request(dataset(0)))
        self.assertFalse(setting.activate(1))
        self.assertTrue(setting.activate(2))
        self.assertTrue(setting.admit(2))

    def test_request_during_calculation_blocks_that_old_save(self):
        options = SessionOptions(TARGET, mode='control', workbook_spec=SPEC, slice_dataset=A)
        excel = self.excel()
        count = 0
        def calculate(*args, **kwargs):
            nonlocal count
            count += 1
            if count == 1:
                options._slice_dataset_setting.request(B)
            return LayerRecommendation(55, 'fixture', {})
        excel.process.side_effect = calculate
        frames = [self.frame('a', 0, False), self.frame('b', 1), self.frame('b', 1, False), self.frame('c', 2)]
        adapter, reports, _ = self.run_case(frames, options, excel)
        self.assertEqual(1, adapter.apply_ir_power.call_count)
        self.assertEqual('c', adapter.apply_ir_power.call_args.args[2])
        self.assertEqual({}, reports[1]['proposed'])
        self.assertEqual({}, reports[1]['applied'])
        excel.reset.assert_called_once()

    def test_admitted_save_finishes_but_second_heater_cannot_use_old_epoch(self):
        options = SessionOptions(TARGET, mode='control', workbook_spec=SPEC, slice_dataset=A)
        excel = self.excel()
        def save(heater, power, key, stop):
            self.assertEqual(1, heater)
            options._slice_dataset_setting.request(B)
        frames = [self.frame('fresh', 1, coupled_output=False)]
        adapter, reports, events = self.run_case(frames, options, excel, apply_hook=save)
        self.assertEqual(1, adapter.apply_ir_power.call_count)
        self.assertEqual({1: 55}, reports[0]['applied'])
        self.assertEqual({}, reports[0]['proposed'])
        self.assertFalse(any(k == 'slice_activated' for k, _ in events))

    def test_newer_request_during_reset_never_activates_intermediate_dataset(self):
        options = SessionOptions(TARGET, mode='preview', workbook_spec=SPEC, slice_dataset=A)
        excel = self.excel()
        def reset():
            if excel.reset.call_count == 1:
                options._slice_dataset_setting.request(C)
        excel.reset.side_effect = reset
        frames = [self.frame('a', 0, False), self.frame('b', 1), self.frame('b', 1, False),
                  self.frame('b', 1, False), self.frame('c', 2)]
        def hook(n, row):
            if n == 2:
                options._slice_dataset_setting.request(B)
        _, _, events = self.run_case(frames, options, excel, hook)
        self.assertEqual([2], [v['epoch'] for k, v in events if k == 'slice_activated'])
        self.assertEqual(100, excel.process.call_args.kwargs['slice_features']['whole_coverage_pct'])

    def test_reset_failure_does_not_continue_with_mixed_history(self):
        options = SessionOptions(TARGET, mode='control', workbook_spec=SPEC, slice_dataset=A)
        excel = self.excel(50)
        excel.reset.side_effect = RuntimeError('reset failed')
        frames = [self.frame('a', 0, False), self.frame('b', 1), self.frame('b', 1, False)]
        def hook(n, row):
            if n == 2:
                options._slice_dataset_setting.request(B)
        with self.assertRaisesRegex(RuntimeError, 'reset failed'):
            self.run_case(frames, options, excel, hook)
        excel.close.assert_called_once()

    def test_clear_handoff_passes_unknown_images_after_new_layer(self):
        options = SessionOptions(TARGET, mode='preview', workbook_spec=SPEC, slice_dataset=A)
        excel = self.excel()
        frames = [self.frame('a', 0, False), self.frame('b', 1), self.frame('b', 1, False), self.frame('c', 2)]
        def hook(n, row):
            if n == 2:
                options._slice_dataset_setting.request(None)
        _, reports, _ = self.run_case(frames, options, excel, hook)
        self.assertIsNone(excel.process.call_args.kwargs['slice_features'])
        self.assertIsNone(reports[-1]['slice_fingerprint'])

    def test_no_valid_snapshot_at_handoff_consumes_first_valid_as_baseline(self):
        options = SessionOptions(TARGET, mode='preview', workbook_spec=SPEC, slice_dataset=A)
        options._slice_dataset_setting.request(B)
        excel = self.excel()
        frames = [self.frame(None, 0, False, t_recoating_c=None), self.frame('a', 5),
                  self.frame('a', 5, False), self.frame('b', 6)]
        self.run_case(frames, options, excel)
        excel.process.assert_called_once()
        self.assertEqual(7, excel.process.call_args.args[0])

    def test_builtin_math_and_history_are_unchanged_by_image_replacement(self):
        frames = [self.frame(str(i), i) for i in range(7)]
        first = SessionOptions(TARGET, mode='preview', slice_dataset=A)
        _, expected, _ = self.run_case(frames, first)
        second = SessionOptions(TARGET, mode='preview', slice_dataset=A)
        def hook(n, row):
            if n in (2, 4):
                second._slice_dataset_setting.request(B if n == 2 else None)
        _, actual, _ = self.run_case(frames, second, hook=hook)
        self.assertEqual([r['proposed'] for r in expected], [r['proposed'] for r in actual])
        self.assertEqual([r['reasons'] for r in expected], [r['reasons'] for r in actual])

    def test_excel_without_image_mapping_keeps_history_and_outputs(self):
        no_images = WorkbookSpec('fixture.xlsx', '', 'B13', 'C13', 'D13', 'B5', 'A13')
        options = SessionOptions(TARGET, mode='preview', workbook_spec=no_images, slice_dataset=A)
        excel = self.excel()
        frames = [self.frame('a', 0, False), self.frame('b', 1), self.frame('c', 2)]
        def hook(n, row):
            if n == 2:
                options._slice_dataset_setting.request(B)
        self.run_case(frames, options, excel, hook)
        excel.reset.assert_not_called()
        self.assertEqual(2, excel.process.call_count)
        self.assertTrue(all(not c.kwargs for c in excel.process.call_args_list))

    def test_queued_stale_proposal_is_hidden_but_actual_history_is_retained(self):
        options = SessionOptions(TARGET, mode='preview', workbook_spec=SPEC, slice_dataset=A)
        controller = SessionController(lambda *args: None, DeferredThread)
        self.assertTrue(controller.connect(options))
        controller.events.put(WorkerEvent(controller.generation, 'snapshot', dict(
            snapshot=self.frame('saved', 2), proposed={1: 55}, reasons={1: 'old'},
            applied={1: 55}, slice_features=A.features(3), slice_epoch=0)))
        controller.update_slice_dataset(B)
        result = controller.poll()[0].payload
        self.assertEqual({}, result['proposed'])
        self.assertEqual({1: 55}, result['applied'])
        self.assertEqual('saved', result['snapshot'].observation_key)
        old = controller.slice_dataset_setting
        controller.thread.complete()
        controller.poll()
        self.assertTrue(controller.connect(SessionOptions(TARGET, mode='preview', workbook_spec=SPEC)))
        old.request(C)
        self.assertEqual((0, None), controller.slice_dataset_setting.active())
        self.assertEqual((0, None), controller.slice_dataset_setting.desired())

    def test_annotation_only_modes_drop_stale_images_but_keep_valid_math(self):
        no_images=WorkbookSpec('fixture.xlsx','','B13','C13','D13','B5','A13')
        for workbook in (None,no_images):
            controller=SessionController(lambda *a:None,DeferredThread)
            controller.connect(SessionOptions(TARGET,mode='preview',workbook_spec=workbook,slice_dataset=A))
            controller.events.put(WorkerEvent(controller.generation,'snapshot',dict(
                snapshot=self.frame('old',1),proposed={1:55},reasons={1:'valid math'},applied={},
                status='preview',slice_features=A.features(2),slice_epoch=0)))
            epoch=controller.update_slice_dataset(B)
            controller.slice_dataset_setting.activate(epoch)
            payload=controller.poll()[0].payload
            self.assertIsNone(payload['slice_features'])
            self.assertEqual({1:55},payload['proposed'])
            self.assertEqual({1:'valid math'},payload['reasons'])
            self.assertEqual('preview',payload['status'])


if __name__ == '__main__':
    unittest.main()
