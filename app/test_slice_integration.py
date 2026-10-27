import tempfile,time,threading,unittest
from pathlib import Path
from unittest.mock import Mock,patch
import tkinter as tk
from PIL import Image
from adapter_contracts import MachineWindow,UiSnapshot,UiAdapter
from layer_algorithm import LayerRecommendation
from session import SessionOptions,run_ui_session
from excel_workbook import WorkbookSpec
from slice_data import SliceConfig,analyze
from standalone_ir import ThermalFeedbackWindow

TARGET=MachineWindow('M1',123,456,'fixture','C:/fixture.exe')
class SliceIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.rootdir=Path(self.tmp.name);self.addCleanup(self.tmp.cleanup)
    def fixtures(self):
        paths=[]
        for n in (1,2):
            p=self.rootdir/f'{n:03}.png';Image.new('L',(100,100),255 if n==2 else 0).save(p);paths.append(p)
        return paths
    def test_session_passes_actual_one_based_layer_and_prior_image_to_excel(self):
        d=analyze(self.fixtures(),SliceConfig(1,400,400,first_layer=1059))
        spec=WorkbookSpec('fixture.xlsx','Sheet','B13','C13','D13','B5','A13','BL13')
        adapter=Mock(spec=UiAdapter);adapter.read_snapshot.side_effect=[UiSnapshot(TARGET,layer_index=k,t_recoating_c=65.,observation_key=str(k),heater1_setting_percent=60.,machine_state='AUTO',observation_is_new=True) for k in (1058,1059)]
        excel=Mock();excel.process.return_value=LayerRecommendation(60.,'test',{})
        stop=threading.Event();reports=[]
        def emit(kind,payload):
            if kind=='snapshot':
                reports.append(payload)
                if len(reports)==2:stop.set()
        with patch('session.WorkbookController',return_value=excel):
            run_ui_session(SessionOptions(TARGET,mode='preview',workbook_spec=spec,slice_dataset=d),stop,emit,lambda:adapter)
        a,b=excel.process.call_args_list
        self.assertEqual(1059,a.args[0]);self.assertEqual(0,a.kwargs['slice_features']['previous_image_valid'])
        self.assertEqual(1060,b.args[0]);self.assertEqual(100,b.kwargs['slice_features']['center_coverage_pct'])
        self.assertEqual(0,b.kwargs['slice_features']['previous_whole_coverage_pct'])
        adapter.apply_ir_power.assert_not_called()
    def test_gui_import_invalidates_old_radius_and_sets_workbook_mapping(self):
        root=tk.Tk();self.addCleanup(root.destroy)
        view=ThermalFeedbackWindow(root,auto_discover=False)
        panel=view.slice_panel
        self.assertEqual(('400','400','흰색'),(panel.dpi_x.get(),panel.dpi_y.get(),panel.polarity.get()))
        panel.radius.set('1');panel.set_paths(self.fixtures())
        deadline=time.monotonic()+4
        while panel.dataset is None and time.monotonic()<deadline:root.update();time.sleep(.01)
        self.assertIsNotNone(panel.dataset)
        old=panel.dataset;panel.radius.set('2')
        self.assertIsNone(panel.dataset)
        deadline=time.monotonic()+4
        while panel.dataset is None and time.monotonic()<deadline:root.update();time.sleep(.01)
        self.assertEqual(2,panel.dataset.config.radius_mm);self.assertNotEqual(old.fingerprint,panel.dataset.fingerprint)
        view._use_slice_workbook('test.xlsx')
        self.assertEqual('BL13',view.workbook_slice_cell.get());self.assertEqual('test.xlsx',view.settings_path.get())
        self.assertEqual('Excel 수식 파일',view.algorithm_type.get())
        panel.clear();self.assertIsNone(panel.dataset);self.assertFalse(panel.paths)
if __name__=='__main__':unittest.main()
