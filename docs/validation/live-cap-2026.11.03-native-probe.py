"""Supplemental synthetic Windows live-cap transport probe.

python live_cap_native_probe.py PACKAGE_ROOT SOURCE_ROOT OUTPUT_JSON
SOURCE_ROOT supplies validation/csv_route_probe.py and CsvRouteFixture.cs from
the pinned reviewed commit. PACKAGE_ROOT is the extracted package from that same revision.
No real machine, browser or equipment is accessed.
"""
import ctypes
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import traceback

COVER = r'''using System; using System.IO; using System.Windows.Forms;
class Cover { [STAThread] static void Main(string[] a) { var f=new Form {Text="Synthetic unrelated foreground",Width=900,Height=700,TopMost=true}; f.Shown+=delegate {f.Activate(); File.WriteAllText(a[0],System.Diagnostics.Process.GetCurrentProcess().Id+","+f.Handle.ToInt64());}; Application.Run(f); } }'''


def adapted(source, folder, package):
    def one(old, new):
        nonlocal source
        if source.count(old) != 1: raise ValueError('Fixture harness mismatch: '+old[:80])
        source = source.replace(old, new, 1)
    one('base=Path(__file__).resolve().parent', 'base=Path('+repr(str(folder))+')')
    one('sys.path.insert(0,str(next(path for path in app_candidates if (path/"windows_ui_adapter.py").is_file())))', 'sys.path.insert(0,'+repr(str(package/'app'))+')')
    one('elif control=="textBoxHeatingOutput":value="62";parameter_type="DoubleParameter";fields[control]="62"',
        'elif control=="textBoxHeatingOutput":value="80";parameter_type="DoubleParameter";fields[control]="80"')
    one('target=MachineWindow(machine,pid,hwnd,"Own CSV fixture",str(exe.resolve()))', 'assert pid==fixture.pid\n        target=MachineWindow(machine,pid,hwnd,"Own CSV fixture",str(exe.resolve()))')
    one('    def make_adapter():\n        adapter=WindowsUiAdapter(source_factory=lambda target:MonitoringCsvReader(target,roots=[monitor]))\n        actual=adapter._request\n        def timed(request,event):\n            begin=time.monotonic()\n            try:return actual(request,event)\n            finally:phase_times.append({"operation":request["operation"],"seconds":round(time.monotonic()-begin,3)})\n        adapter._request=timed\n        return adapter',
        '    def make_adapter():\n        from resilient_adapter import ResilientUiAdapter\n        def own_discovery():\n            assert fixture.poll() is None and fixture.pid==target.pid\n            assert Path(target.image_path).resolve()==exe.resolve()\n            return [target]\n        def raw_factory(source_factory):\n            adapter=WindowsUiAdapter(source_factory=source_factory)\n            actual=adapter._request\n            def timed(request,event):\n                begin=time.monotonic()\n                error=None\n                try:return actual(request,event)\n                except Exception as exc:\n                    error={"type":type(exc).__name__,"message":str(exc)}\n                    raise\n                finally:phase_times.append({"operation":request["operation"],"seconds":round(time.monotonic()-begin,3),"error":error})\n            adapter._request=timed\n            return adapter\n        wrapper=ResilientUiAdapter(raw_factory=raw_factory, discoverer=own_discovery,\n            source_factory=lambda target:MonitoringCsvReader(target,roots=[monitor]))\n        record_wrapper(wrapper)\n        return wrapper')
    one('options=SessionOptions(target,mode="control")',
        'options=SessionOptions(target,mode="control",maximum_ir_percent=80)')
    one('try:run_ui_session(options,stop,lambda kind,payload:events.put((kind,payload)),make_adapter)',
        'try:\n                from observed_session import run_observed_session\n                run_observed_session(options,stop,lambda kind,payload:events.put((kind,payload)),adapter_factory=make_adapter)')
    begin=source.index('        if case=="dirty":\n            receive(expect_error=True)')
    end=source.index('        assert not Path(str(marker)+".table-access").exists()',begin)
    source=source[:begin]+'''        receive(initial_layer)
        assert before.power == 80
        # The production wrapper requires an authenticated baseline and a newer
        # fresh row before declaring control ready; offline snapshots do not count.
        ready_layer=None; candidate=0; deadline=time.monotonic()+40
        while time.monotonic()<deadline:
            candidate+=1
            command(layer=candidate+1)
            with csv_path.open("ab") as stream:
                stream.write(rows_bytes([record(candidate,42,datetime.now().replace(microsecond=0))]))
            payload=receive(candidate)
            assert int(Path(str(marker)+".saves").read_text())==0, "Save before live-cap request"
            cap_status=options._maximum_ir_setting.status()
            baseline=sequence_persistence.capture(target)
            assert baseline.power==80 and baseline.non_ir==before.non_ir
            if (payload["snapshot"].control_available and not cap_status['pending']
                    and cap_status['active_stamp']==cap_status['requested_stamp']
                    and cap_status['active_effective_cap']==80):
                ready_layer=candidate;break
        assert ready_layer is not None, "Production wrapper did not reach healthy control and active 80% cap"
        command(page=page,minimized=minimized)
        cover_info=start_cover(pid,hwnd)
        assert options._maximum_ir_setting.set(60)
        receive(ready_layer)
        assert int(Path(str(marker)+".saves").read_text())==0, "cap change saved baseline"
        actuals=[]; save_counts=[]; snapshots=[]
        for offset, expected in enumerate((75,70,65,60),1):
            layer=ready_layer+offset
            command(layer=layer+1)
            with csv_path.open("ab") as stream:
                stream.write(rows_bytes([record(layer,42,datetime.now().replace(microsecond=0))]))
            append_times[layer]=time.monotonic()
            payload=receive(layer)
            assert payload["snapshot"].t_recoating_c==42
            current=sequence_persistence.capture(target)
            actuals.append(current.power)
            save_counts.append(int(Path(str(marker)+".saves").read_text()))
            snapshots.append(payload.get("effective_mode"))
            assert current.power==expected, (layer,expected,current.power)
            assert current.non_ir==before.non_ir
            assert save_counts[-1]==offset, save_counts
            receive(layer)
            receive(layer)
            assert int(Path(str(marker)+".saves").read_text())==offset, "duplicate row caused Save"
        background_state=json.loads(Path(str(marker)+".background-state").read_text(encoding="utf-8-sig"))
        assert background_state["page"]==page and background_state["minimized"]==minimized, background_state
        expected_saves=4
''' +source[end:]
    one('"completeCsvToSessionSeconds":[round(value,3) for value in received],"uiOperations":phase_times}',
        '"completeCsvToSessionSeconds":[round(value,3) for value in received],"uiOperations":phase_times, "powers":actuals, "save_counts":save_counts, "snapshots":snapshots, "background_state":background_state, "baseline_save_count":0, "baseline_cap_status":cap_status, "baseline_layer":ready_layer}')
    one('print(json.dumps(result),flush=True);return result',
        'result.update(stop_cover(*cover_info)); return result')
    one('    finally:\n        stop.set()\n        if worker is not None:worker.join(6)',
        '    finally:\n        stop.set()\n        if worker is not None:worker.join(6)\n        capture_failure(marker, locals().get("target"), locals().get("before"), fixture, phase_times)')
    return source


def main(package, source_root, output):
    report={'passed':False,'cases':[], 'scope':'Synthetic target, synthetic CSV and temporary settings; live cap set after connected 80% baseline; no equipment.',
            'untested':['Real BJT, hardware, thermal response, locked desktop.', 'Discovery uses a fixture-scoped identity-checked callback; native discovery is not exercised.']}
    cover=None; cover_active=None; last_capture={}; wrapper_events=[]; stop_sample=threading.Event(); samples=[]
    try:
        if os.name!='nt': raise RuntimeError('Windows required')
        sys.path.insert(0,str(package/'app'))
        import sequence_persistence
        harness=source_root/'validation/csv_route_probe.py'; fixture_src=source_root/'validation/CsvRouteFixture.cs'
        report['harness_sha256']=hashlib.sha256(harness.read_bytes()).hexdigest()
        report['fixture_source_sha256']=hashlib.sha256(fixture_src.read_bytes()).hexdigest()
        report['session_sha256']=hashlib.sha256((package/'app/session.py').read_bytes()).hexdigest()
        report['probe_sha256']=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        report['helper_sha256']=hashlib.sha256((package/'app/native_helper.ps1').read_bytes()).hexdigest()
        report['bridge_sha256']=hashlib.sha256((package/'app/UiaBridge.dll').read_bytes()).hexdigest()
        with tempfile.TemporaryDirectory(prefix='livecap-native-') as tmp:
            folder=Path(tmp); (folder/'fullroutebin').mkdir()
            compiler=Path(os.environ['WINDIR'])/'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
            exe=folder/'fullroutebin/CsvRouteFixture.exe'; cover_exe=folder/'cover.exe'; cover_src=folder/'cover.cs'
            cover_src.write_text(COVER)
            for dest, src, refs in ((exe,fixture_src,('System.Windows.Forms.dll','System.Drawing.dll','System.Web.Extensions.dll')),(cover_exe,cover_src,('System.Windows.Forms.dll','System.Drawing.dll'))):
                subprocess.run([str(compiler),'/nologo','/target:winexe','/platform:x64','/out:'+str(dest)]+['/reference:'+r for r in refs]+[str(src)],check=True,capture_output=True,timeout=30)
            user32=ctypes.windll.user32; user32.GetForegroundWindow.restype=ctypes.c_void_p
            def start_cover(target_pid,target_hwnd):
                nonlocal cover,cover_active,samples
                ready=folder/'cover.ready';ready.unlink(missing_ok=True)
                cover=subprocess.Popen([str(cover_exe),str(ready)])
                until=time.monotonic()+10
                while not ready.exists() and time.monotonic()<until:
                    if cover.poll() is not None: raise RuntimeError('Cover exited')
                    time.sleep(.05)
                if not ready.exists(): raise TimeoutError('Cover startup timed out')
                pid,hwnd=map(int,ready.read_text().split(',')); assert pid==cover.pid and pid!=target_pid
                until=time.monotonic()+5
                while user32.GetForegroundWindow()!=hwnd and time.monotonic()<until:time.sleep(.05)
                assert user32.GetForegroundWindow()==hwnd,'Cover did not gain foreground'
                samples=[];stop_sample.clear()
                def sample():
                    while not stop_sample.wait(.02):samples.append(user32.GetForegroundWindow()==hwnd)
                thread=threading.Thread(target=sample,daemon=True);thread.start()
                cover_active=(thread,hwnd,pid,target_pid,target_hwnd);return cover_active
            def stop_cover(thread,hwnd,pid,target_pid,target_hwnd):
                nonlocal cover,cover_active
                stop_sample.set();thread.join(2)
                result={'foreground_maintained':bool(samples) and all(samples) and user32.GetForegroundWindow()==hwnd,
                        'foreground_samples':len(samples),'cover_pid':pid,'cover_hwnd':hwnd,'target_pid':target_pid,'target_hwnd':target_hwnd}
                cover.terminate();cover.wait(timeout=5);cover=None;cover_active=None
                return result
            def record_wrapper(wrapper):
                original=wrapper._outage
                def record(reason, **kwargs):
                    wrapper_events.append({'event':'outage','reason':str(reason),'monotonic':time.monotonic()})
                    return original(reason, **kwargs)
                wrapper._outage=record
            def capture_failure(marker,target,before,fixture,phase_times):
                nonlocal last_capture
                last_capture={'fixture_pid':fixture.pid,'fixture_running_before_teardown':fixture.poll() is None, 'ui_operations':list(phase_times), 'wrapper_outages':list(wrapper_events)}
                try:last_capture['saves']=Path(str(marker)+'.saves').read_text()
                except Exception as exc:last_capture['saves_error']=str(exc)
                try:
                    snap=sequence_persistence.capture(target)
                    last_capture['power']=snap.power
                    last_capture['non_ir_unchanged']=before is not None and snap.non_ir==before.non_ir
                    last_capture['non_ir_sha256']=hashlib.sha256(repr(sorted(snap.non_ir.items())).encode()).hexdigest()
                    last_capture['baseline_non_ir_sha256']=hashlib.sha256(repr(sorted(before.non_ir.items())).encode()).hexdigest() if before else None
                except Exception as exc:last_capture['persistence_error']=str(exc)
            namespace={'__file__':str(folder/'harness.py'),'__name__':'live_cap_fixture', 'start_cover':start_cover,'stop_cover':stop_cover,'capture_failure':capture_failure,'record_wrapper':record_wrapper}
            exec(compile(adapted(harness.read_text(encoding='utf-8'),folder,package),str(harness),'exec'),namespace)
            for machine in ('M1','S3'):
                for minimized in (False,True):
                    last_capture={};wrapper_events.clear()
                    try:
                        result=namespace['exercise'](machine,'webcam',minimized,'normal')
                        result['passed']=result['passed'] and result['foreground_maintained']
                    except Exception as exc:
                        result={'machine':machine,'minimized':minimized,'passed':False,'error':str(exc),'traceback':traceback.format_exc()}
                        if cover_active is not None:result.update(stop_cover(*cover_active))
                        elif cover is not None:
                            cover.terminate();cover.wait(timeout=5);cover=None
                    result['readonly_pre_teardown']=last_capture
                    report['cases'].append(result)
            report['passed']=len(report['cases'])==4 and all(c['passed'] for c in report['cases'])
    except Exception as exc:
        report['error']=str(exc);report['traceback']=traceback.format_exc()
    finally:
        stop_sample.set()
        if cover is not None:cover.terminate();cover.wait(timeout=5)
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(report,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    print(json.dumps(report,indent=2,ensure_ascii=True))
    return 0 if report['passed'] else 1

if __name__=='__main__':
    if len(sys.argv)!=4:raise SystemExit(__doc__)
    raise SystemExit(main(*(Path(x).resolve() for x in sys.argv[1:])))
