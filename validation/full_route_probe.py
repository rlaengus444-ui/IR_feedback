"""Real UIA test of a synthetic fixture only; never selects another process."""
from pathlib import Path
import json
import subprocess
import sys
import threading
import time

base=Path(__file__).parent
app_candidates=(base.parent/"standalone-ir"/"app",base.parent/"app",base.parent.parent/"app")
app_path=next((path for path in app_candidates if (path/"windows_ui_adapter.py").is_file()),None)
if app_path is None:raise RuntimeError("The sibling standalone-ir/app or packaged app directory is missing")
sys.path.insert(0,str(app_path))
from adapter_contracts import MachineWindow, AmbiguousWriteError, ControlUnavailable
from windows_ui_adapter import WindowsUiAdapter
import sequence_persistence
from sequence_field_audit import field_bindings, SequenceFieldAuditError

def build_fields(exe,machine,marker):
    configs=exe.parent/"Machines"/machine/"Configs"
    configs.mkdir(parents=True,exist_ok=True)
    owners={"BJT":[],"MachineProfile":[]}
    fields={}
    for control,(scope,name,kind) in field_bindings(machine).items():
        if kind=="toggle":
            value=True;parameter_type="BoolParameter";fields[control]="toggle:1"
        elif control=="textBoxHeatingOutput":
            value="62";parameter_type="DoubleParameter";fields[control]="62"
        elif name.startswith("Printhead") and name.endswith("Port"):
            value="fixture-port";parameter_type="StringParameter";fields[control]=value
        else:
            value="5";parameter_type="IntParameter" if "Count" in name or "Interval" in name else "DoubleParameter";fields[control]=value
        owner="BJT" if scope=="Printing" else "MachineProfile"
        owners[owner].append({"Scope":scope,"Name":name,"Type":parameter_type,"Value":value})
    for owner,entries in owners.items():
        (configs/(owner+".parameters.json")).write_text(json.dumps(entries),encoding="utf-8")
    field_path=Path(str(marker)+".fields.json")
    field_path.write_text(json.dumps(fields),encoding="utf-8")
    return field_path

def exercise(machine,mode):
    started=time.perf_counter()
    marker=base/("full-route-"+machine+"-"+mode)
    for suffix in (".window",".saves",".edited",".error",".trace",".heartbeat",".initial-viewport",".stack",".fields-state",".sequence-clicks"):
        Path(str(marker)+suffix).unlink(missing_ok=True)
    exe=base/"fullroutebin"/"FullRouteFixture.exe"
    field_path=build_fields(exe,machine,marker)
    fixture_args=[str(exe),str(marker),machine,mode,str(field_path)]
    fixture=subprocess.Popen(fixture_args,creationflags=subprocess.CREATE_NO_WINDOW)
    adapter=WindowsUiAdapter()
    phase_times=[]
    original_request=adapter._request
    def timed_request(request,event):
        phase_start=time.perf_counter()
        try:return original_request(request,event)
        finally:phase_times.append({"operation":request["operation"],"seconds":round(time.perf_counter()-phase_start,3)})
    adapter._request=timed_request
    event=threading.Event()
    try:
        deadline=time.monotonic()+4
        window=Path(str(marker)+".window")
        while not window.exists() and time.monotonic()<deadline:time.sleep(.025)
        pid,hwnd=map(int,window.read_text().split(","))
        target=MachineWindow(machine,pid,hwnd,"BinderJet synthetic Sequence fixture",str(exe.resolve()))
        adapter.connect(target,event)
        if mode in ("dirty","dirtysequence"):
            try:adapter.prepare_mode("control",event)
            except (ControlUnavailable,SequenceFieldAuditError):pass
            else:raise AssertionError("Pre-existing non-IR edit must reject control")
            assert Path(str(marker)+".saves").read_text()=="0"
            assert sequence_persistence.capture(target).power==62
            initial_fields=json.loads(field_path.read_text(encoding="utf-8"))
            state_file=Path(str(marker)+".fields-state")
            deadline=time.monotonic()+1
            while not state_file.exists() and time.monotonic()<deadline:time.sleep(.025)
            readback=json.loads(state_file.read_text(encoding="utf-8-sig"))
            assert readback["textBoxHeatingOutput"]=="62"
            assert any(readback[key]!=value for key,value in initial_fields.items() if key!="textBoxHeatingOutput")
            result={"machine":machine,"case":mode,"passed":True,"saveCount":0,"unsavedEditPreserved":True,"elapsedSeconds":round(time.perf_counter()-started,3)}
            if mode=="dirtysequence":
                assert Path(str(marker)+".sequence-clicks").read_text()=="0"
                result["sequenceNavigationCount"]=0
            print(json.dumps(result));return result
        adapter.prepare_mode("control",event)
        baseline=sequence_persistence.capture(target)
        snapshot=adapter.read_snapshot(event)
        if mode=="late":
            deadline=time.monotonic()+20
            while snapshot.observation_key is None and time.monotonic()<deadline:
                event.wait(1)
                snapshot=adapter.read_snapshot(event)
        assert snapshot.control_available,snapshot.control_unavailable_reason
        expected_layer=857 if mode=="late" else 0
        assert snapshot.layer_index==expected_layer and snapshot.t_recoating_c==42,snapshot
        followed_viewport=None
        if mode=="late":
            assert Path(str(marker)+".initial-viewport").read_text()=="0"
            until=time.monotonic()+1
            while time.monotonic()<until:
                heartbeat=Path(str(marker)+".heartbeat")
                if heartbeat.exists():
                    followed_viewport=int(heartbeat.read_text().split("row=")[1].split()[0])
                    if followed_viewport>0:break
                time.sleep(.025)
            assert followed_viewport is not None and abs(857-followed_viewport)<=128,followed_viewport
        if mode in ("success","late","bootstrap"):
            adapter.apply_ir_power(1,67,snapshot.observation_key,event)
            persisted=sequence_persistence.capture(target)
            assert persisted.power==67 and persisted.non_ir==baseline.non_ir,persisted
            after=adapter.read_snapshot(event)
            assert after.layer_index==expected_layer and after.machine_state=="AUTO",after
        else:
            try:adapter.apply_ir_power(1,67,snapshot.observation_key,event)
            except AmbiguousWriteError:pass
            else:raise AssertionError("Unacknowledged Save must be ambiguous")
            try:adapter.apply_ir_power(1,67,snapshot.observation_key,event)
            except ControlUnavailable:pass
            else:raise AssertionError("Ambiguous Save must never be retried")
            if mode=="savetardy":
                deadline=time.monotonic()+8
                while sequence_persistence.capture(target).power!=67 and time.monotonic()<deadline:time.sleep(.05)
                persisted=sequence_persistence.capture(target)
                assert persisted.power==67 and persisted.non_ir==baseline.non_ir
        expected_saves=0 if mode in ("edithang","statechange") else 1
        assert Path(str(marker)+".saves").read_text()==str(expected_saves)
        if mode in ("edithang","statechange"):
            assert sequence_persistence.capture(target).power==62
            assert Path(str(marker)+".edited").read_text()=="67"
        elapsed=round(time.perf_counter()-started,3)
        result={"machine":machine,"case":mode,"passed":True,"saveCount":expected_saves,"nonIrUnchanged":True,"elapsedSeconds":elapsed,"uiOperations":phase_times}
        if mode=="late":result.update(initialViewportRow=0,followingViewportRow=followed_viewport,observedRow=857,cachePrefetchRadius=128)
        print(json.dumps(result))
        return result
    finally:
        error=Path(str(marker)+".error")
        if error.exists():print("FIXTURE ERROR",error.read_text(encoding="utf-8-sig"))
        adapter.disconnect()
        if fixture.poll() is None:fixture.kill()
        fixture.wait(timeout=2)

if __name__=="__main__":
    if any(argument not in ("--late","--bootstrap") for argument in sys.argv[1:]):
        raise SystemExit("Usage: full_route_probe.py [--late|--bootstrap]")
    cases=[("M1","success"),("S3","success"),("M1","savehang"),("M1","savetardy"),("M1","edithang"),("M1","statechange"),("M1","dirty"),("S3","dirty")]
    if "--late" in sys.argv:cases=[("M1","late")]
    if "--bootstrap" in sys.argv:cases=[("M1","bootstrap"),("S3","bootstrap"),("M1","dirtysequence"),("S3","dirtysequence")]
    results=[exercise(*case) for case in cases]
    result_name="late-layer-results.json" if "--late" in sys.argv else "full-route-results.json"
    if "--bootstrap" in sys.argv:result_name="bootstrap-results.json"
    (base/result_name).write_text(json.dumps(results,indent=2),encoding="utf-8")
