"""Packaged native, user-pointed Home reading and bounded content proofs; synthetic only."""
import argparse
from contextlib import contextmanager
import ctypes
from dataclasses import replace
from datetime import datetime,timedelta
import csv
import hashlib
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import threading
import time

SOURCE=Path(__file__).resolve().parents[1]/'validation/AutoSliceSourceFixture.cs'


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('package_root',type=Path);parser.add_argument('output',type=Path)
    args=parser.parse_args();root=args.package_root.resolve()
    report={'passed':False,'checks':{},'physical_equipment_actions':0,
            'actual_bjt_path_binding_tested':False,'excel_com_tested':False}
    proc=None;source=None;adapter=None
    @contextmanager
    def owned_temporary():
        temporary=tempfile.TemporaryDirectory(prefix='auto-source-native-')
        try:
            yield temporary.name
        finally:
            if source is not None:source.stop()
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try:proc.wait(timeout=5)
                except subprocess.TimeoutExpired:proc.kill();proc.wait(timeout=5)
            temporary.cleanup()
    try:
        if os.name!='nt' or Path(sys.executable).resolve()!=root/'runtime/python.exe':
            raise ValueError('Run using the packaged Windows runtime')
        sys.path.insert(0,str(root/'app'))
        from PIL import Image
        from adapter_contracts import MachineWindow,SourceSuspended,UiSnapshot
        from csv_source import JobContext,MonitoringCsvReader
        from auto_slice_source import AutoSliceSource
        from session import SliceDatasetSetting
        from slice_data import SliceConfig
        from windows_slice_source import WindowsPathReader,BindingLost,SourceReadError,canonical
        from windows_ui_adapter import WindowsUiAdapter
        import windows_slice_source
        assert Path(windows_slice_source.__file__).resolve()==root/'app/windows_slice_source.py'
        adapter=WindowsUiAdapter()
        with owned_temporary() as temporary:
            folder=Path(temporary);a=folder/'a';b=folder/'b';a.mkdir();b.mkdir()
            for directory,ink in ((a,0),(b,255)):
                for n in range(1,9):Image.new('L',(32,32),ink).save(directory/f'{n:03}.bmp')
            compiler=Path(os.environ['WINDIR'])/'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
            exe=folder/'AutoSliceSourceFixture.exe';marker=folder/'fixture'
            subprocess.run([str(compiler),'/nologo','/target:winexe','/platform:x64','/out:'+str(exe),
                '/reference:System.Windows.Forms.dll','/reference:System.Drawing.dll',str(SOURCE)],
                check=True,capture_output=True,timeout=30)
            proc=subprocess.Popen([str(exe),str(marker),str(a/'001.bmp'),str(b/'001.bmp')])
            def wait(predicate,timeout=8):
                end=time.monotonic()+timeout
                while time.monotonic()<end:
                    try:
                        if predicate():return
                    except (OSError,ValueError):pass
                    if proc.poll() is not None:raise RuntimeError('Synthetic UI exited')
                    time.sleep(.025)
                raise TimeoutError('Synthetic auto-source check timed out')
            wait(lambda:Path(str(marker)+'.ready').exists(),20)
            def handles():
                return list(map(int,Path(str(marker)+'.handles').read_text().split(',')))
            def command(action):
                done=Path(str(marker)+'.done');done.unlink(missing_ok=True)
                temp=Path(str(marker)+'.command.tmp');temp.write_text(action,encoding='utf-8')
                os.replace(temp,Path(str(marker)+'.command'))
                wait(lambda:done.exists() and done.read_text(encoding='utf-8')==action)
            pid,hwnd,home,box,label,machine,wrong,px,py,lx,ly=handles()
            assert pid==proc.pid
            target=MachineWindow('M1',pid,hwnd,'Owned auto-source fixture',str(exe))
            reader=WindowsPathReader();checks=report['checks']
            binding,selection=reader.bind_at(target,px,py)
            checks['explicit_pointed_edit_bound']=binding.leaf==box and canonical(selection.directory)==canonical(a)
            static_binding,static_selection=reader.bind_at(target,lx,ly)
            checks['explicit_pointed_static_bound']=static_binding.leaf==label and canonical(static_selection.directory)==canonical(a)
            report['label_native_class']=reader._class(label)
            label_meta=Path(str(marker)+'.label-meta').read_text().split(',')
            checks['ellipsized_noneditable_label_retains_complete_native_text']=(label_meta[:2]==['True','True']
                and static_selection.raw_text==str(a/'001.bmp'))
            command('label_disabled')
            disabled_binding,disabled_selection=reader.bind_at(target,lx,ly)
            checks['disabled_noneditable_label_point_binding']=(disabled_binding.leaf==label
                and disabled_selection.raw_text==str(a/'001.bmp'))
            command('label_enabled')
            for test_target,handle,name in ((replace(target,pid=pid+1),box,'wrong_pid_rejected'),
                                            (target,wrong,'process_view_rejected')):
                try:reader.bind_handle(test_target,handle);checks[name]=False
                except SourceReadError:checks[name]=True
            command('hide')
            checks['hidden_home_remains_readable']=canonical(reader.read(binding).directory)==canonical(a)
            times=[]
            for _ in range(100):
                started=time.perf_counter();reader.read(binding);times.append(time.perf_counter()-started)
            report['native_path_read_ms']={'median':statistics.median(times)*1000,'maximum':max(times)*1000,'samples':len(times)}
            checks['cached_path_read_is_bounded']=max(times)<.5
            def proof(directory,bound=binding):
                value=bound.token(str(directory))
                value['files']=[{'path':str(directory/f'{n:03}.bmp'),
                                'sha256':hashlib.sha256((directory/f'{n:03}.bmp').read_bytes()).hexdigest()} for n in (1,2)]
                return value
            def verify(value):
                return adapter._request({'operation':'verify_slice_source','pid':pid,'hwnd':hwnd,
                    'machine':'M1','handles':{'machine':machine},'source_guard':value},threading.Event())
            good_a=proof(a)
            checks['production_helper_hidden_source_and_two_hashes']=verify(good_a)['success']
            label_proof=proof(a,static_binding)
            checks['noneditable_label_native_source_proof']=verify(label_proof)['success']
            command('label_ellipsis')
            try:reader.read(static_binding);checks['literal_ellipsis_label_text_rejected']=False
            except SourceReadError:checks['literal_ellipsis_label_text_rejected']=True
            try:verify(label_proof);checks['literal_ellipsis_label_native_proof_holds']=False
            except SourceSuspended:checks['literal_ellipsis_label_native_proof_holds']=True
            command('a')
            kernel=ctypes.WinDLL('kernel32',use_last_error=True)
            kernel.GetLongPathNameW.argtypes=[ctypes.c_wchar_p,ctypes.c_wchar_p,ctypes.c_uint]
            kernel.GetLongPathNameW.restype=ctypes.c_uint
            long_name=ctypes.create_unicode_buffer(32768)
            if not kernel.GetLongPathNameW(str(a),long_name,len(long_name)):
                raise OSError('Fixture long-path name could not be read')
            report['fixture_short_alias_differs']=canonical(a)!=canonical(long_name.value)
            command('path:'+str(Path(long_name.value)/'001.bmp'))
            checks['long_and_short_native_path_forms_match']=verify(good_a)['success']
            command('a')
            command('reparent')
            try:verify(good_a);checks['same_home_native_reparent_holds']=False
            except SourceSuspended:checks['same_home_native_reparent_holds']=True
            command('restore_parent');verify(good_a)
            command('change_id')
            try:verify(good_a);checks['native_control_id_change_holds']=False
            except SourceSuspended:checks['native_control_id_change_holds']=True
            command('restore_id');verify(good_a)
            bad_class=dict(good_a);bad_class['chain']=[list(node) for node in binding.chain]
            bad_class['chain'][0][1]='not-the-bound-native-class'
            try:verify(bad_class);checks['native_class_mismatch_holds']=False
            except SourceSuspended:checks['native_class_mismatch_holds']=True
            command('path:Image path: '+str(a/'002.bmp'))
            checks['current_filename_change_preserves_folder']=canonical(reader.read(binding).directory)==canonical(a)
            checks['native_path_grammar_matches_python']=verify(good_a)['success']
            source=AutoSliceSource(target,SliceConfig(1,25.4,25.4,filename_origin=1),binding,
                reader=reader,poll_seconds=.05,stable_seconds=.05,scan_seconds=.05,
                clock=lambda:datetime.now()-timedelta(seconds=60))
            setting=SliceDatasetSetting();source.start(setting)
            wait(lambda:source.view()['ready'])
            first_epoch=source.view()['epoch']
            checks['automatic_import_without_csv']=len(source.view()['dataset'].records)==8
            command('path:'+str(a/'003.bmp'));time.sleep(.2)
            checks['current_filename_does_not_reanalyze_set']=source.view()['epoch']==first_epoch
            # A real parsed CSV uses a long name while UI/import may use8.3.
            monitor=folder/'Monitoring';monitor.mkdir();stamp=datetime.now().replace(microsecond=0)
            started=stamp-timedelta(minutes=1)
            context=JobContext('synthetic-alias',0,started,0,'AUTO')
            class FixedLog:
                def read(self):return context
            job=monitor/(started.strftime('%Y%m%d_%H%M%S')+'_8-layer');job.mkdir()
            csv_path=job/('LayerDatasheet_'+started.strftime('%Y%m%d_%H%M%S')+'.csv')
            with csv_path.open('w',encoding='utf-8-sig',newline='') as stream:
                writer=csv.writer(stream);writer.writerow(list(MonitoringCsvReader.REQUIRED)+['ImgSlicing'])
                writer.writerow(['1',started.strftime('%m/%d/%Y %H:%M:%S'),
                    (stamp-timedelta(seconds=20)).strftime('%m/%d/%Y %H:%M:%S'),
                    stamp.strftime('%m/%d/%Y %H:%M:%S'),'65',str(Path(long_name.value)/'001.bmp')])
            csv_reader=MonitoringCsvReader(target,roots=[monitor],log=FixedLog())
            parsed=csv_reader.read().sample
            view=source.view();setting.activate(view['epoch'])
            row=UiSnapshot(target,layer_index=parsed.layer_index,observation_key=parsed.key,t_recoating_c=parsed.temperature,
                slicing_image_path=parsed.slicing_image_path,recoating_finished=parsed.recoating_finished,
                slice_lineage=parsed.slice_lineage,csv_generation=parsed.source_generation)
            def verify_lineage():
                fresh=csv_reader.read().sample
                return fresh.key==parsed.key and fresh.slice_lineage==parsed.slice_lineage
            admitted,reason=source.frame(row,view['epoch'],view['dataset'],verify_lineage=verify_lineage)
            checks['parsed_csv_long_alias_matches_imported_full_path']=(admitted is not None and admitted.guard is not None and source.admit(admitted))
            command('b')
            wait(lambda:source.view()['path']==canonical(b))
            wait(lambda:source.view()['ready'] and source.view()['epoch']!=first_epoch)
            checks['ui_change_refreshes_set_without_csv']=source.view()['dataset'].records[0].whole_coverage_pct==100
            try:verify(good_a);checks['pre_edit_folder_mismatch_holds']=False
            except SourceSuspended:checks['pre_edit_folder_mismatch_holds']=True
            good_b=proof(b);before=(b/'001.bmp').stat()
            Image.new('L',(32,32),0).save(b/'001.bmp')
            os.utime(b/'001.bmp',ns=(before.st_atime_ns,before.st_mtime_ns))
            checks['same_size_mtime_collision_fixture']=(b/'001.bmp').stat().st_size==before.st_size and (b/'001.bmp').stat().st_mtime_ns==before.st_mtime_ns
            try:verify(good_b);checks['content_hash_rejects_same_stat_change']=False
            except SourceSuspended:checks['content_hash_rejects_same_stat_change']=True
            wait(lambda:source.view()['needs_reload'] and source.view()['epoch'] is not None)
            checks['same_path_reanalyzes_but_waits_reload_ack']=not source.view()['ready']
            source.confirm_reload();checks['explicit_reload_ack_reopens_source_gate']=source.view()['ready']
            command('a');wait(lambda:source.view()['ready'] and source.view()['path']==canonical(a))
            old_binding=binding;command('recreate')
            try:reader.read(old_binding);checks['recreated_field_needs_rebind']=False
            except BindingLost:checks['recreated_field_needs_rebind']=True
            wait(lambda:not source.view()['bound'])
            command('show');_,_,_,new_box,*_=handles()
            binding,selection=reader.bind_handle(target,new_box)
            source.rebind(binding);wait(lambda:source.view()['ready'])
            checks['explicit_rebind_recovers_without_reconnecting']=True
            # Real content-proof timing with two4MiB images, bounded by helper.
            bench=folder/'bench';bench.mkdir()
            for n in (1,2):Image.new('L',(2048,2048),n*70).save(bench/f'{n:03}.bmp')
            source.stop();source=None;command('path:'+str(bench/'001.bmp'))
            sample=[]
            for _ in range(3):sample.append(verify(proof(bench,binding))['source_proof_ms'])
            report['two_image_content_proof_ms']={'median':statistics.median(sample),'samples':sample,
                'total_bytes':sum(p.stat().st_size for p in bench.glob('*.bmp'))}
            checks['content_proof_within_production_budget']=max(sample)<=750
            extension_directory=folder/'layers.png';extension_directory.mkdir()
            for n in (1,2):Image.new('L',(32,32),n*70).save(extension_directory/f'{n:03}.bmp')
            command('path:'+str(extension_directory/'001.bmp'))
            checks['extension_named_parent_folder_supported']=verify(proof(extension_directory,binding))['success']
            command('path:'+str(extension_directory))
            directory_binding,directory_selection=reader.bind_handle(target,new_box)
            checks['extension_named_directory_field_supported']=(directory_binding.directory_field and
                canonical(directory_selection.directory)==canonical(extension_directory) and
                verify(proof(extension_directory,directory_binding))['success'])
            heartbeat=Path(str(marker)+'.heartbeat')
            ticks,saves,navs=heartbeat.read_text().split(',');time.sleep(.25)
            next_ticks,next_saves,next_navs=heartbeat.read_text().split(',')
            checks['target_heartbeat_responsive']=int(next_ticks)>int(ticks)
            checks['zero_save_or_navigation']=saves==navs==next_saves==next_navs=='0'
            checks['no_process_grid_messages']=not Path(str(marker)+'.poison').exists()
            report['passed']=all(checks.values())
    except Exception as error:
        import traceback
        report.update(error=str(error),traceback=traceback.format_exc())
    finally:
        if source is not None:source.stop()
        if adapter is not None:adapter.disconnect()
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:proc.wait(timeout=5)
            except subprocess.TimeoutExpired:proc.kill();proc.wait(timeout=5)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False,indent=2))
    return 0 if report['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
