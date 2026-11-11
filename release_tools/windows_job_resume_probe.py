"""Owned Windows fixtures: same-PID new job, real process/PID replacement, native job races."""
import json, os, queue, subprocess, sys, tempfile, threading, time, traceback
from datetime import datetime,timedelta
from pathlib import Path
from windows_hidden_session_probe import fields_for, csv_bytes, wait_until, sha256, FLAGS


def exercise(exe,folder,machine):
    from adapter_contracts import MachineWindow
    from csv_source import MonitoringCsvReader
    from observed_session import run_observed_session
    from resilient_adapter import ResilientUiAdapter
    from windows_ui_adapter import WindowsUiAdapter
    from session import SessionOptions
    from layer_charts import LayerHistory
    import sequence_persistence
    marker=folder/machine
    spec,_=fields_for(exe,machine,marker)
    monitor=folder/('monitor-'+machine);monitor.mkdir()
    logs=exe.parent/'Logs';logs.mkdir(exist_ok=True)
    log=logs/('20261027_'+machine+'.log')
    events=queue.Queue();stop=threading.Event();worker=None;fixture=None
    raw_calls=[];reports=[];rebinds=[];barriers=[];history=LayerHistory()
    run=[None]; csv_path=[None]; start=[None]; next_layer=[0]; phase=['initial'];race=[False];expire=[False]
    result={'machine':machine,'passed':False,'phases':[]}
    def command(**values):
        done=Path(str(marker)+'.command-done');done.unlink(missing_ok=True)
        temp=Path(str(marker)+'.command.tmp');temp.write_text(json.dumps(values))
        os.replace(temp,Path(str(marker)+'.command'))
        wait_until(lambda:done.exists() and json.loads(done.read_text(encoding='utf-8-sig'))==values,5,'Fixture command timeout')
    def launch():
        Path(str(marker)+'.window').unlink(missing_ok=True)
        p=subprocess.Popen([str(exe),str(marker),machine,'backgroundcsv',str(spec),'taskbar'],creationflags=FLAGS)
        wait_until(lambda:Path(str(marker)+'.window').exists() or p.poll() is not None,10,'Fixture launch')
        assert p.poll() is None
        pid,hwnd=map(int,Path(str(marker)+'.window').read_text().split(','));assert pid==p.pid
        return p,MachineWindow(machine,pid,hwnd,'Owned restart fixture',str(exe.resolve()))
    def start_job(stamp):
        start[0]=stamp; next_layer[0]=0
        prefix=stamp.strftime('%Y-%m-%d%p%I%M%S')+'...[BJT - AUTO]: '
        with log.open('a',encoding='utf-8') as f:f.write(prefix+'AutoPrint started!\n'+prefix+'Layer: 0 -> Current Z = 1\n')
        job=monitor/(stamp.strftime('%Y%m%d_%H%M%S')+'_100-layer');job.mkdir()
        csv_path[0]=job/('LayerDatasheet_'+stamp.strftime('%Y%m%d_%H%M%S')+'.csv')
        csv_path[0].write_bytes(csv_bytes([list(MonitoringCsvReader.REQUIRED)]))
    def append():
        layer=next_layer[0];next_layer[0]+=1
        command(layer=layer+1)
        now=datetime.now().replace(microsecond=0)
        prefix=now.strftime('%Y-%m-%d%p%I%M%S')+'...[BJT - AUTO]: '
        with log.open('a',encoding='utf-8') as f:f.write(prefix+f'Layer: {layer+1} -> Current Z = 1\n')
        # Synthetic row preserves real timestamp/CSV grammar and delayed completion.
        finish=now; recoating=max(start[0],finish-timedelta(seconds=1))
        row=[str(layer+1),start[0].strftime('%m/%d/%Y %H:%M:%S'),recoating.strftime('%m/%d/%Y %H:%M:%S'),finish.strftime('%m/%d/%Y %H:%M:%S'),'42']
        with csv_path[0].open('ab') as f:f.write(csv_bytes([row]))
        return layer
    def emit(kind,value):
        if kind=='snapshot':
            snap=value['snapshot'];history.record(value)
            reports.append({'phase':phase[0],'run':snap.run_key,'layer':snap.layer_index,'ready':snap.control_available,
                'actual_pid':snap.actual_target.pid if snap.actual_target else None,'epoch':snap.control_epoch,
                'applied':value.get('applied',{}),'status':value.get('status'),'reason':snap.control_unavailable_reason})
        elif kind=='target_rebound':rebinds.append({'pid':value.pid,'hwnd':value.hwnd})
        elif kind in ('error','observer_hold','actuation_barrier'):barriers.append((kind,str(value)))
        events.put((kind,value))
    def raw_factory(**kw):
        raw=WindowsUiAdapter(**kw);original=raw._request
        def request(data,event):
            entry={'phase':phase[0],'operation':data['operation'],'pid':data.get('pid')};begin=time.monotonic()
            if expire[0] and data['operation']=='apply':
                expire[0]=False
                time.sleep(5.1)  # Completed calculation, no helper/edit started.
                entry['deadline_expired_before_native_edit']=True
            if race[0] and data['operation']=='apply':
                race[0]=False
                prefix=datetime.now().strftime('%Y-%m-%d%p%I%M%S')+'...[BJT - AUTO]: '
                with log.open('a',encoding='utf-8') as f:f.write(prefix+'AutoPrint started!\n'+prefix+'Layer: 0 -> Current Z = 1\n')
                entry['job_switched_after_python_preflight']=True
            try:
                value=original(data,event);entry['success']=True
                if data['operation']=='discover':entry['windows']=value.get('windows',[])
                return value
            except Exception as e:
                entry['success']=False;entry['error']=str(e)
                entry['error_type']=type(e).__name__
                entry['edit_marker']=Path(data['edit_marker']).exists() if 'edit_marker' in data else False
                entry['save_marker']=Path(data['save_marker']).exists() if 'save_marker' in data else False
                raise
            finally:entry['seconds']=time.monotonic()-begin;raw_calls.append(entry)
        raw._request=request;return raw
    def wait_snapshot(layer, timeout=15):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            try:kind,value=events.get(timeout=min(1,max(.01,deadline-time.monotonic())))
            except queue.Empty:continue
            if kind in ('error','observer_hold','actuation_barrier'):raise AssertionError((kind,value))
            if kind=='snapshot' and value['snapshot'].layer_index==layer:return value
        raise AssertionError('No observation '+str(layer))
    def saves():return int(Path(str(marker)+'.saves').read_text())
    def phase_save(name,cap):
        phase[0]=name
        wait_until(lambda:options._maximum_ir_setting.status()['ready'],10,'Session output bounds were not initialized')
        options._maximum_ir_setting.set(cap);before=saves()
        deadline=time.monotonic()+100
        while time.monotonic()<deadline:
            layer=append();value=wait_snapshot(layer)
            # Give stable full audits/candidate reads time without expiring data.
            time.sleep(.35)
            if saves()>before:
                assert value['snapshot'].run_key==wrapper._pinned_job
                assert not wrapper._sticky
                result['phases'].append({'phase':name,'saved':saves()-before,'layer':layer,'pid':wrapper.current_target.pid})
                return
        raise AssertionError('No automatic Save in '+name)
    try:
        log.write_text('',encoding='utf-8')
        # Old baseline permits all initial bootstrap gates before fresh rows.
        start_job(datetime.now().replace(microsecond=0)-timedelta(minutes=2))
        fixture,target=launch();original_pid=target.pid
        selector=raw_factory(source_factory=lambda t:None)
        try:found=[x for x in selector.discover() if x.pid==original_pid]
        finally:selector.disconnect()
        result['initial_selection']={'expected_hwnd':target.hwnd,'candidates':[dict(machine=x.machine,pid=x.pid,hwnd=x.hwnd,image_path=x.image_path) for x in found]}
        assert len(found)==1 and found[0].machine==machine and found[0].hwnd==target.hwnd
        assert os.path.samefile(found[0].image_path,exe)
        target=found[0]  # Use the exact identity the operator selects in the UI.
        options=SessionOptions(target,mode='control',maximum_ir_percent=80)
        # Actual native negatives for Kind-dedup equivalence. The remaining
        # Read checks still reject class/role/PID/scope errors before any edit.
        from adapter_contracts import AdapterError,ObservationSuspended
        negative_checks=[];phase[0]='native-field-negatives';negative_saves=saves()
        raw=raw_factory(source_factory=lambda t:MonitoringCsvReader(t,roots=[monitor]))
        raw.connect(target,stop);raw.prepare_mode('control',stop)
        for name in ('unknown-kind','text-as-toggle','pushbutton-as-checkbox','outside-scope','wrong-pid'):
            layer=append();source=raw._csv.read()
            token=source.token
            assert source.sample and token
            mapping={k:dict(v) for k,v in raw._native_fields.items()}
            data=dict(operation='verify_control',pid=target.pid,hwnd=target.hwnd,
                machine=machine,image_path=target.image_path,handles=dict(raw._native_handles),
                field_map=mapping,csv_token=token,observation=dict(row_index=source.sample.layer_index))
            field=mapping['textBoxHeatingOutput']
            if name=='unknown-kind':field['kind']='unsupported'
            elif name=='text-as-toggle':field['kind']='toggle'
            elif name=='pushbutton-as-checkbox':field.update(kind='toggle',hwnd=raw._native_handles['save'])
            elif name=='outside-scope':field['hwnd']=raw._native_handles['state']
            elif name=='wrong-pid':data['pid']+=100000
            try:raw._request(data,stop)
            except AdapterError as error:
                assert not isinstance(error,ObservationSuspended)
                negative_checks.append({'case':name,'type':type(error).__name__,'message':str(error)})
            else:raise AssertionError('Native field guard accepted '+name)
            assert saves()==negative_saves and not raw._control_fault
        result['native_field_negative_checks']=negative_checks
        raw.disconnect()
        wrapper=ResilientUiAdapter(raw_factory=raw_factory,source_factory=lambda t:MonitoringCsvReader(t,roots=[monitor]))
        def work():
            try:run_observed_session(options,stop,emit,adapter_factory=lambda:wrapper)
            except BaseException as e:events.put(('error',str(e)))
        worker=threading.Thread(target=work,daemon=True);worker.start()
        phase_save('initial-save',75)
        phase[0]='deadline-pre-edit';options._maximum_ir_setting.set(70)
        expire[0]=True;before_expiry=saves();expiry_epoch=wrapper._epoch
        expiry_deadline=time.monotonic()+45
        while expire[0] and time.monotonic()<expiry_deadline:
            layer=append();wait_snapshot(layer)
        assert not expire[0], 'No admitted apply request available for the bounded expiry injection'
        assert saves()==before_expiry and wrapper._epoch==expiry_epoch and not wrapper._sticky
        expired_calls=[x for x in raw_calls if x.get('deadline_expired_before_native_edit')]
        assert len(expired_calls)==1 and not expired_calls[0]['edit_marker'] and not expired_calls[0]['save_marker']
        assert expired_calls[0]['error_type']=='ObservationExpired'
        phase_save('deadline-next-fresh-auto-resume',70)
        assert wrapper._epoch==expiry_epoch and not wrapper._sticky
        result['deadline_expiry_recovered_without_epoch_reset']=True
        first_run=wrapper._pinned_job;first_reader=wrapper._reader;first_watermark=dict(wrapper._watermark)
        first_power=sequence_persistence.capture(target).power
        phase[0]='stop-start'
        command(state='IDLE');prefix=datetime.now().strftime('%Y-%m-%d%p%I%M%S')+'...[BJT - AUTO]: '
        with log.open('a',encoding='utf-8') as f:f.write(prefix+'AutoPrint cancelled.\n')
        wait_until(lambda:any(x['phase']=='stop-start' and '비자동' in str(x['status']) for x in reports),10,'IDLE not observed')
        before=saves();start_job(datetime.now().replace(microsecond=0));command(state='AUTO')
        phase_save('same-pid-new-job',65)
        assert wrapper._pinned_job!=first_run and first_run in wrapper._retired_jobs
        assert wrapper.current_target.pid==original_pid and wrapper._reader is first_reader
        assert all(wrapper._watermark.get(k)==v for k,v in first_watermark.items())
        assert saves()>before
        # Actual owned process exit, replacement PID and native production discovery.
        phase[0]='process-exit';fixture.terminate();fixture.wait(timeout=5)
        wait_until(lambda:wrapper._raw is None,15,'Transport outage not detected')
        # Fixture reads display values from its spec, so refresh only this test spec
        # from persisted controller config before launch, exactly like a restart.
        current=sequence_persistence.capture(target)
        data=json.loads(spec.read_text());data['textBoxHeatingOutput']=str(current.power);spec.write_text(json.dumps(data))
        fixture,new_target=launch();assert new_target.pid!=original_pid
        phase_save('new-pid-same-job',55)
        assert wrapper.current_target.pid==new_target.pid and wrapper._reader is first_reader
        assert any(x['pid']==new_target.pid for x in rebinds)
        assert first_run in wrapper._retired_jobs
        # Switch jobs after the final Python CSV preflight, before native edit.
        phase[0]='native-pre-edit-job-switch';options._maximum_ir_setting.set(45);race[0]=True;before=saves()
        for _ in range(8):
            layer=append();wait_snapshot(layer)
            if not race[0]:break
        assert not race[0] and saves()==before
        injected=[x for x in raw_calls if x.get('job_switched_after_python_preflight')]
        assert len(injected)==1 and not injected[0]['success']
        assert not injected[0]['edit_marker'] and not injected[0]['save_marker']
        assert not wrapper._sticky
        # Re-establish another verified run, then inject a switch from inside the
        # fixture's native IR TextChanged handler, between edit and Save.
        time.sleep(1.05);start_job(datetime.now().replace(microsecond=0))
        phase_save('after-native-race-new-job',40)
        phase[0]='native-pre-save-job-switch';options._maximum_ir_setting.set(30);command(jobSwitchOnEdit=True);before=saves()
        for _ in range(8):
            layer=append()
            try:wait_snapshot(layer)
            except AssertionError:
                if wrapper._sticky:break
                raise
            if wrapper._sticky:break
        assert wrapper._sticky and saves()==before
        assert Path(str(marker)+'.job-switched-after-edit').exists()
        failed=[x for x in raw_calls if x['phase']==phase[0] and x['operation']=='apply' and not x.get('success')]
        assert failed and failed[-1]['edit_marker'] and not failed[-1]['save_marker']
        assert not Path(str(marker)+'.table-access').exists()
        assert worker.is_alive()
        result.update(passed=True,original_pid=original_pid,replacement_pid=new_target.pid,
            native_discovery_used=True,shared_reader_preserved=True,watermarks_preserved=True,
            new_job_auto_resumed=True,pid_auto_resumed=True,native_pre_edit_rejected=True,
            native_pre_save_rejected=True,sticky_after_uncertain_edit=True,actual_pid_events=rebinds,
            saved_before_stop=before,barriers=barriers)
    except BaseException as e:
        result.update(error=type(e).__name__+': '+str(e),traceback=traceback.format_exc(),barriers=barriers)
    finally:
        result['snapshots']=reports;result['native_calls']=raw_calls
        stop.set()
        if worker:worker.join(10);result['worker_retired']=not worker.is_alive()
        if fixture and fixture.poll() is None:fixture.terminate();fixture.wait(timeout=5)
    return result


def main(package,source,output):
    report={'passed':False,'cases':[],'scope':'Owned temporary full-profile Windows fixtures; no equipment or Excel COM.'}
    try:
        assert os.name=='nt';sys.path.insert(0,str(package/'app'))
        with tempfile.TemporaryDirectory(prefix='job-pid-native-') as temp:
            folder=Path(temp);binary=folder/'binary';binary.mkdir();exe=binary/'BinderJetFixture.exe'
            compiler=Path(os.environ['WINDIR'])/'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
            fixture_source=source/'validation/CsvRouteFixture.cs'
            subprocess.run([str(compiler),'/nologo','/target:winexe','/platform:x64','/out:'+str(exe),
                '/reference:System.Windows.Forms.dll','/reference:System.Drawing.dll','/reference:System.Web.Extensions.dll',str(fixture_source)],check=True,capture_output=True,text=True)
            report['fixture_sha256']=sha256(fixture_source);report['probe_sha256']=sha256(__file__)
            for machine in ('M1','S3'):report['cases'].append(exercise(exe,folder,machine))
            report['passed']=all(x['passed'] and x['worker_retired'] for x in report['cases']) and len(report['cases'])==2
    except BaseException as e:report.update(error=str(e),traceback=traceback.format_exc())
    output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=True,indent=2));return 0 if report['passed'] else 1

if __name__=='__main__':raise SystemExit(main(*(Path(x).resolve() for x in sys.argv[1:])))
