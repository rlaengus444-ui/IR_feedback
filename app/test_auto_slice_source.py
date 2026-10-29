"""Actual files, delayed CSVs and deterministic source-swap/admission races."""
import csv
from dataclasses import replace
from datetime import datetime, timedelta
import hashlib
import io
import os
from pathlib import Path
import queue
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from PIL import Image
from adapter_contracts import MachineWindow, UiSnapshot, UiAdapter, SourceSuspended
from auto_slice_source import AutoSliceSource, content_digest, inventory
from csv_source import CsvSnapshot, JobContext, MonitoringCsvReader
from excel_workbook import WorkbookSpec
from layer_algorithm import LayerRecommendation
from session import SessionOptions, SliceDatasetSetting, run_ui_session
from slice_data import SliceConfig, analyze
from windows_slice_source import BindingLost, PathBinding, SourceReadError, SourceSelection, canonical, parse_path_text
from windows_ui_adapter import WindowsUiAdapter


TARGET=MachineWindow('M1',11,12,'fixture','C:/fixture/Operator.exe')
CONFIG=SliceConfig(1,25.4,25.4,filename_origin=1)
SPEC=WorkbookSpec('fixture.xlsx','','B13','C13','D13','B5','A13','BL13')


class FakeReader:
    def __init__(self,directory):self.directory=str(directory);self.error=None;self.reads=0
    def read(self,binding):
        self.reads+=1
        if self.error:raise self.error
        return SourceSelection(self.directory,self.directory,binding)


class PathTextTests(unittest.TestCase):
    def test_strict_folder_and_current_filename_normalize_without_role_guessing(self):
        for text in ('C:\\작업 폴더\\slices','"C:\\작업 폴더\\slices\\001.png"',
                     'Image path: C:/작업 폴더/slices/002.PNG'):
            self.assertEqual('c:\\작업 폴더\\slices',canonical(parse_path_text(text)))
        self.assertEqual('\\\\server\\share\\images',canonical(parse_path_text('\\\\server\\share\\images\\001.png')))

    def test_ambiguous_relative_ellipsized_zip_and_unknown_prefix_are_rejected(self):
        for value in ('', 'images/001.png','C:\\a -> D:\\b','C:\\a\\…\\001.png',
                      'C:\\a\\set.zip','output: C:\\camera','\\\\?\\C:\\a'):
            with self.subTest(value=value),self.assertRaises(SourceReadError):parse_path_text(value)


class AutoSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.a=self.root/'a';self.b=self.root/'b'
        self.a.mkdir();self.b.mkdir()
        for folder,ink in ((self.a,0),(self.b,255)):
            for n in range(1,9):Image.new('L',(16,16),ink).save(folder/f'{n:03}.bmp')
        self.tick=0.;self.now=datetime(2026,10,29,10,0,0)
        self.binding=PathBinding(TARGET,100,200,(), 'Home')
        self.reader=FakeReader(self.a)
        self.service=AutoSliceSource(TARGET,CONFIG,self.binding,reader=self.reader,
            clock=lambda:self.now+timedelta(seconds=self.tick),monotonic=lambda:self.tick,
            stable_seconds=.2,scan_seconds=0)
        self.service._setting=SliceDatasetSetting()
        self.addCleanup(self.service.stop)

    def prime(self):
        self.service.poll_once();self.tick+=.5;self.service.poll_once()
        job=self.service._jobs.get_nowait();self.service.analyze_job(job)
        epoch,dataset=self.service._setting.desired()
        self.assertTrue(self.service._setting.activate(epoch))
        return epoch,dataset

    def frame(self,layer,folder=None,stamp=None,**kwargs):
        return UiSnapshot(TARGET,layer_index=layer-1,t_recoating_c=65,
            observation_key=f'row-{layer}',heater1_setting_percent=50,machine_state='AUTO',
            control_available=True,observation_is_new=True,
            slicing_image_path=str((folder or self.reader.directory)/Path(f'{layer:03}.bmp')),
            recoating_finished=stamp or self.now+timedelta(seconds=20),slice_lineage=f'lineage-{layer}',**kwargs)

    def test_initial_import_and_path_change_do_not_wait_for_any_csv(self):
        epoch,a=self.prime();self.assertEqual(8,len(a.records));self.assertTrue(self.service.view()['ready'])
        self.reader.directory=str(self.b);self.service.poll_once()
        self.assertFalse(self.service.view()['ready'])
        self.tick+=.5;self.service.poll_once();self.service.analyze_job(self.service._jobs.get_nowait())
        new_epoch,b=self.service._setting.desired()
        self.assertGreater(new_epoch,epoch);self.assertEqual(100,b.records[0].whole_coverage_pct)
        self.assertEqual(canonical(str(self.b/'001.bmp')),canonical(self.service._source_paths[1]))

    def test_delayed_old_measurement_and_other_folder_never_admit(self):
        epoch,data=self.prime()
        for frame in (self.frame(2,stamp=self.now),self.frame(2,folder=self.b),
                      replace(self.frame(2),recoating_finished=None),replace(self.frame(2),slice_lineage='')):
            self.assertIsNone(self.service.frame(frame,epoch,data)[0])
        admission,_=self.service.frame(self.frame(2),epoch,data)
        self.assertTrue(self.service.admit(admission))

    def test_equal_basename_in_another_directory_is_not_same_source(self):
        epoch,data=self.prime()
        self.assertIsNone(self.service.frame(self.frame(3,folder=self.b),epoch,data)[0])

    def test_previous_image_and_powder_run_are_not_backfilled_across_switch(self):
        epoch,data=self.prime()
        first,_=self.service.frame(self.frame(5),epoch,data)
        self.assertEqual(0,first.features['previous_image_valid'])
        self.assertEqual(0,first.features['previous_powder_layers'])
        second,_=self.service.frame(self.frame(6),epoch,data)
        self.assertEqual(1,second.features['previous_image_valid'])
        self.assertEqual(1,second.features['previous_powder_layers'])
        gap,_=self.service.frame(self.frame(8),epoch,data)
        self.assertEqual(0,gap.features['previous_image_valid'])
        self.assertEqual(0,gap.features['previous_powder_layers'])

    def test_fixed_filename_origin_survives_partial_folder_copy(self):
        folder=self.root/'copy';folder.mkdir()
        Image.new('L',(16,16),0).save(folder/'005.bmp')
        first=analyze([folder/'005.bmp'],CONFIG)
        Image.new('L',(16,16),0).save(folder/'001.bmp')
        second=analyze([folder/'005.bmp',folder/'001.bmp'],CONFIG)
        self.assertEqual([5],[r.layer for r in first.records])
        self.assertEqual([1,5],[r.layer for r in second.records])

    def test_a_b_a_cancels_obsolete_jobs_even_if_content_is_identical(self):
        epoch,data=self.prime()
        original,_=self.service.frame(self.frame(2),epoch,data)
        self.reader.directory=str(self.b);self.service.poll_once();self.tick+=.5;self.service.poll_once()
        b_job=self.service._jobs.get_nowait()
        self.reader.directory=str(self.a);self.service.poll_once();self.tick+=.5;self.service.poll_once()
        a_job=self.service._jobs.get_nowait()
        self.service.analyze_job(b_job)
        self.assertFalse(self.service.view()['ready'])
        self.service.analyze_job(a_job)
        new_epoch,new_data=self.service._setting.desired()
        self.assertGreater(new_epoch,epoch);self.assertEqual(data.fingerprint,new_data.fingerprint)
        self.assertFalse(self.service.admit(original))

    def test_same_size_restored_mtime_is_found_by_content_and_requires_reload(self):
        epoch,data=self.prime();path=self.a/'001.bmp';before=path.stat()
        Image.new('L',(16,16),255).save(path);os.utime(path,ns=(before.st_atime_ns,before.st_mtime_ns))
        self.assertEqual(before.st_size,path.stat().st_size)
        self.assertEqual(inventory(self.a),self.service._manifest)
        self.service.background_digest_once()
        self.assertFalse(self.service.view()['ready']);self.assertTrue(self.service.view()['needs_reload'])
        self.tick+=.5;self.service.poll_once();self.service.analyze_job(self.service._jobs.get_nowait())
        self.assertFalse(self.service.view()['ready'])
        self.service.confirm_reload();self.assertTrue(self.service.view()['ready'])
        new_epoch,new_data=self.service._setting.desired()
        self.assertEqual(100,new_data.records[0].whole_coverage_pct)
        self.assertIsNone(self.service.frame(self.frame(2,stamp=self.now),new_epoch,new_data)[0])

    def test_same_stat_rewrite_during_explicit_refresh_requires_reload_before_ready(self):
        self.prime();self.service.refresh();self.tick+=.5;self.service.poll_once()
        work=self.service._jobs.get_nowait();path=self.a/'001.bmp';before=path.stat()
        Image.new('L',(16,16),255).save(path);os.utime(path,ns=(before.st_atime_ns,before.st_mtime_ns))
        self.service.analyze_job(work)
        self.assertEqual(100,self.service.view()['dataset'].records[0].whole_coverage_pct)
        self.assertTrue(self.service.view()['needs_reload']);self.assertFalse(self.service.view()['ready'])

    def test_path_field_loss_requires_explicit_rebind(self):
        self.prime();self.reader.error=BindingLost('recreated');self.service.poll_once()
        self.reader.error=None;self.service.poll_once()
        self.assertIsNone(self.service.binding);self.assertFalse(self.service.view()['ready'])
        self.service.rebind(self.binding);self.prime();self.assertTrue(self.service.view()['ready'])

    def test_watcher_lag_closes_admission_and_requires_new_publication(self):
        epoch,data=self.prime();admission,_=self.service.frame(self.frame(2),epoch,data)
        self.tick+=3
        self.assertFalse(self.service.admit(admission))
        self.assertIsNone(self.service.frame(self.frame(3),epoch,data)[0])
        self.assertFalse(self.service.view()['ready'])

    def test_expired_admission_never_revives_after_watcher_reads_again(self):
        epoch,data=self.prime();old,_=self.service.frame(self.frame(2),epoch,data)
        self.tick+=3;self.assertFalse(self.service.admit(old));version=self.service.view()['version']
        self.service.poll_once();self.assertFalse(self.service.admit(old))
        self.assertGreaterEqual(self.service.view()['version'],version)
        self.tick+=.5;self.service.poll_once();self.service.analyze_job(self.service._jobs.get_nowait())
        self.assertTrue(self.service.view()['ready']);self.assertFalse(self.service.admit(old))

    def test_watcher_itself_detects_missed_lease_before_refreshing_heartbeat(self):
        epoch,data=self.prime();old,_=self.service.frame(self.frame(2),epoch,data)
        self.tick+=3;self.service.poll_once()
        self.assertFalse(self.service.admit(old));self.assertFalse(self.service.view()['ready'])

    def test_source_history_does_not_cross_job_csv_generation_or_corrected_layer(self):
        epoch,data=self.prime()
        self.service.frame(replace(self.frame(5),run_key='A',csv_generation='file1'),epoch,data)
        first_b=replace(self.frame(6),run_key='B',csv_generation='file2')
        self.assertIsNone(self.service.frame(first_b,epoch,data)[0])
        new,_=self.service.frame(first_b,epoch,data)
        self.assertEqual(0,new.features['previous_image_valid'])
        self.assertEqual(0,new.features['previous_powder_layers'])
        corrected=replace(first_b,slice_lineage='corrected')
        self.assertIsNone(self.service.frame(corrected,epoch,data)[0])
        self.assertIsNone(self.service.frame(corrected,epoch,data)[0])
        next_row=replace(self.frame(7),run_key='B',csv_generation='file2')
        next_admission,_=self.service.frame(next_row,epoch,data)
        self.assertEqual(0,next_admission.features['previous_image_valid'])
        # Same job, replacement CSV still forms a new provenance boundary.
        new_file=replace(self.frame(8),run_key='B',csv_generation='file3')
        self.assertIsNone(self.service.frame(new_file,epoch,data)[0])
        new,_=self.service.frame(new_file,epoch,data)
        self.assertEqual(0,new.features['previous_image_valid'])

    def test_cancellation_keeps_watching_but_does_not_restart_same_analysis(self):
        self.prime();self.service.cancel_analysis();self.tick+=.5;self.service.poll_once()
        with self.assertRaises(queue.Empty):self.service._jobs.get_nowait()
        self.service.refresh();self.tick+=.5;self.service.poll_once()
        self.assertIsNotNone(self.service._jobs.get_nowait())

    def test_no_source_io_or_late_publication_after_stop(self):
        self.service.poll_once();self.tick+=.5;self.service.poll_once();job=self.service._jobs.get_nowait()
        self.service.stop();self.service.analyze_job(job)
        self.assertIsNone(self.service._setting.desired()[1])

    def run_session(self,frames,excel,apply_error=None,before_read=None):
        if self.service._setting is None:self.service._setting=SliceDatasetSetting()
        epoch,data=self.prime()
        options=SessionOptions(TARGET,mode='control',workbook_spec=SPEC,auto_source=self.service)
        def start(setting):
            self.service._setting=setting
            self.service._epoch=setting.request(data,force=True)
        self.service.start=start
        adapter=Mock(spec=UiAdapter);position=iter(enumerate(frames))
        def read(_):
            index,value=next(position)
            if before_read:before_read(index,value)
            return value
        adapter.read_snapshot.side_effect=read
        if apply_error:adapter.apply_ir_power.side_effect=apply_error
        class FastStop(threading.Event):
            def wait(self,timeout=None):return self.is_set()
        stop=FastStop();reports=[]
        def emit(kind,value):
            if kind=='snapshot':
                reports.append(value)
                if len(reports)==len(frames):stop.set()
        with patch('session.WorkbookController',return_value=excel):
            run_ui_session(options,stop,emit,lambda:adapter)
        return adapter,reports

    def test_session_delayed_old_csv_then_new_baseline_then_fresh_layer(self):
        frames=[self.frame(2,stamp=self.now),replace(self.frame(2),observation_key='baseline'),
                replace(self.frame(2),observation_key='baseline',observation_is_new=False),self.frame(3)]
        excel=Mock();excel.process.return_value=LayerRecommendation(55,'fixture',{})
        adapter,reports=self.run_session(frames,excel)
        self.assertEqual(1,excel.process.call_count)
        self.assertEqual(3,excel.process.call_args.args[0])
        self.assertEqual(1,excel.process.call_args.kwargs['slice_features']['previous_powder_layers'])
        adapter.apply_ir_power.assert_called_once()
        self.assertEqual('lineage-3',adapter.apply_ir_power.call_args.kwargs['source_guard']['lineage'])
        self.assertTrue(all(not r['applied'] for r in reports[:3]))

    def test_source_change_during_excel_calculation_blocks_that_save(self):
        excel=Mock()
        def calculate(*args,**kwargs):
            self.service.rebind(self.binding)
            return LayerRecommendation(55,'fixture',{})
        excel.process.side_effect=calculate
        adapter,reports=self.run_session([self.frame(2),self.frame(3)],excel)
        self.assertEqual(1,excel.process.call_count)
        adapter.apply_ir_power.assert_not_called()
        self.assertEqual({},reports[-1]['proposed'])

    def test_source_pre_edit_hold_does_not_enter_sequence_recovery(self):
        excel=Mock();excel.process.return_value=LayerRecommendation(55,'fixture',{})
        adapter,reports=self.run_session([self.frame(2),self.frame(3),self.frame(4)],excel,
                                         SourceSuspended('image content changed'))
        adapter.apply_ir_power.assert_called_once();adapter.suspend_control.assert_not_called()
        self.assertIn('이미지',reports[1]['status']);self.assertEqual({},reports[1]['applied'])

    def test_nonready_row_cannot_become_save_when_exact_cached_row_is_restored(self):
        for variant in ('missing_ir','UNKNOWN'):
            with self.subTest(variant=variant):
                # Each scenario uses a new source, because session cleanup stops it.
                self.service=AutoSliceSource(TARGET,CONFIG,self.binding,reader=self.reader,
                    clock=lambda:self.now+timedelta(seconds=self.tick),monotonic=lambda:self.tick,
                    stable_seconds=.2,scan_seconds=0)
                bad=replace(self.frame(4),slicing_image_path=str(self.b/'004.bmp'))
                nonready=replace(self.frame(6),**({'heater1_setting_percent':None} if variant=='missing_ir' else {'machine_state':'UNKNOWN'}))
                restored=replace(self.frame(6),observation_is_new=False)
                frames=[self.frame(2),self.frame(3),bad,self.frame(5),nonready,restored,self.frame(7)]
                excel=Mock();excel.process.side_effect=lambda layer,*a,**k:LayerRecommendation(55 if layer==7 else 50,'fixture',{})
                adapter,_=self.run_session(frames,excel)
                self.assertNotIn(6,[c.args[0] for c in excel.process.call_args_list])
                self.assertEqual([7],[c.args[0] for c in excel.process.call_args_list if c.args[0]>=5])
                adapter.apply_ir_power.assert_called_once()
                self.assertEqual('row-7',adapter.apply_ir_power.call_args.args[2])

    def test_normal_idle_between_layers_does_not_reset_history_or_skip_each_layer(self):
        idle=replace(self.frame(3),observation_key=None,observation_is_new=False)
        frames=[self.frame(2),self.frame(3),idle,idle,self.frame(4)]
        excel=Mock();excel.process.return_value=LayerRecommendation(50,'hold',{})
        adapter,_=self.run_session(frames,excel)
        self.assertEqual([3,4],[c.args[0] for c in excel.process.call_args_list])
        self.assertEqual(1,excel.reset.call_count)  # Initial complete-set handoff only.
        adapter.apply_ir_power.assert_not_called()

    def test_equal_power_rows_cannot_learn_unverified_same_stat_image_data(self):
        def mutate(index,frame):
            if index==1:
                path=self.a/'003.bmp';stat=path.stat()
                Image.new('L',(16,16),255).save(path)
                os.utime(path,ns=(stat.st_atime_ns,stat.st_mtime_ns))
        excel=Mock();excel.process.return_value=LayerRecommendation(50,'hold',{})
        adapter,_=self.run_session([self.frame(2),self.frame(3),self.frame(4)],excel,before_read=mutate)
        excel.process.assert_not_called();adapter.apply_ir_power.assert_not_called()
        self.assertTrue(self.service.view()['needs_reload'])
        self.assertNotIn(3,self.service._known_powder)

    def test_prelearning_lineage_change_does_not_promote_image_history(self):
        epoch,data=self.prime()
        candidate,_=self.service.frame(self.frame(3),epoch,data,verify_lineage=lambda:False)
        self.assertIsNone(candidate);self.assertNotIn(3,self.service._known_powder)
        self.assertNotIn(3,self.service._observed_rows)

    def test_source_change_during_proof_prevents_compare_and_commit(self):
        epoch,data=self.prime();expected=data._by_layer[3].sha256
        def change(*args,**kwargs):
            self.service.rebind(self.binding)
            return expected
        with patch('auto_slice_source.content_digest',side_effect=change):
            candidate,_=self.service.frame(self.frame(3),epoch,data)
        self.assertIsNone(candidate);self.assertEqual({},self.service._known_powder)

    def test_calculation_time_expired_lease_cannot_save_or_revive_cached_row(self):
        excel=Mock()
        def calculate(layer,*args,**kwargs):
            if layer==3:self.tick+=3
            return LayerRecommendation(55,'fixture',{})
        excel.process.side_effect=calculate
        def restore(index,frame):
            if index==2:
                self.service.poll_once();self.tick+=.5;self.service.poll_once()
                self.service.analyze_job(self.service._jobs.get_nowait())
        frames=[self.frame(2),self.frame(3),replace(self.frame(3),observation_is_new=False),self.frame(4)]
        adapter,reports=self.run_session(frames,excel,before_read=restore)
        adapter.apply_ir_power.assert_called_once()
        self.assertEqual('row-4',adapter.apply_ir_power.call_args.args[2])
        self.assertEqual({},reports[1]['applied']);self.assertEqual({},reports[2]['applied'])

    def test_path_watcher_keeps_running_during_blocked_image_analysis(self):
        entered=threading.Event();release=threading.Event();calls=[]
        def slow(paths,config,**kwargs):
            calls.append(paths)
            if len(calls)==1:
                entered.set();release.wait(3)
            return analyze(paths,config,**kwargs)
        service=AutoSliceSource(TARGET,CONFIG,self.binding,reader=self.reader,analyzer=slow,
            poll_seconds=.01,stable_seconds=.01,scan_seconds=.01)
        self.addCleanup(service.stop);self.addCleanup(release.set)
        service.start(SliceDatasetSetting())
        self.assertTrue(entered.wait(2));before=service.view()['version']
        self.reader.directory=str(self.b)
        deadline=time.monotonic()+2
        while service.view()['version']==before and time.monotonic()<deadline:time.sleep(.01)
        self.assertGreater(service.view()['version'],before)
        self.assertFalse(service.view()['ready'])
        release.set();deadline=time.monotonic()+2
        while not service.view()['ready'] and time.monotonic()<deadline:time.sleep(.01)
        self.assertTrue(service.view()['ready'])
        self.assertEqual(100,service.view()['dataset'].records[0].whole_coverage_pct)


class AutoLineageTests(unittest.TestCase):
    def test_same_semantic_csv_key_cannot_hide_changed_image_or_recoat_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);now=datetime.now().replace(microsecond=0)
            context=JobContext('run',0,now-timedelta(minutes=1),0,'AUTO')
            log=Mock();log.read.return_value=context
            job=root/(context.first_started.strftime('%Y%m%d_%H%M%S')+'_8-layer');job.mkdir()
            path=job/('LayerDatasheet_'+context.first_started.strftime('%Y%m%d_%H%M%S')+'.csv')
            header=list(MonitoringCsvReader.REQUIRED)+['ImgSlicing']
            def encode(row):
                text=io.StringIO(newline='');csv.writer(text).writerows(row);return text.getvalue().encode()
            row=['1',context.first_started.strftime('%m/%d/%Y %H:%M:%S'),
                 (now-timedelta(seconds=20)).strftime('%m/%d/%Y %H:%M:%S'),
                 now.strftime('%m/%d/%Y %H:%M:%S'),'65','C:\\a\\001.png']
            path.write_bytes(encode([header,row]))
            reader=MonitoringCsvReader(TARGET,roots=[root],log=log)
            first=reader.read();self.assertIsNotNone(first.sample)
            changed=list(row);changed[-1]='C:\\b\\001.png'
            with path.open('ab') as f:f.write(encode([changed]))
            second=reader.read()
            self.assertEqual(first.sample.key,second.sample.key)
            self.assertNotEqual(first.sample.slice_lineage,second.sample.slice_lineage)
            adapter=WindowsUiAdapter();adapter._csv=reader;adapter._run_key='run'
            adapter._new_sample_key=first.sample.key
            with self.assertRaises(SourceSuspended):adapter._verify_csv_observation(first.sample.key,first.sample.slice_lineage)
            self.assertIsNotNone(adapter._verify_csv_observation(first.sample.key))


if __name__=='__main__':unittest.main()
