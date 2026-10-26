"""Actual adapter/session/algorithm, fake CSV/controls, forbidden table access."""
import csv
from datetime import datetime,timedelta
import io
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time

base=Path(__file__).resolve().parent
app_candidates=(base.parent/"standalone-ir"/"app",base.parent/"app",base.parent.parent/"app")
sys.path.insert(0,str(next(path for path in app_candidates if (path/"windows_ui_adapter.py").is_file())))
from adapter_contracts import MachineWindow
from csv_source import MonitoringCsvReader
from sequence_field_audit import field_bindings
import sequence_persistence
from session import SessionOptions,run_ui_session
from windows_ui_adapter import WindowsUiAdapter


def rows_bytes(rows):
    text=io.StringIO(newline="");csv.writer(text,lineterminator="\r\n").writerows(rows)
    return text.getvalue().encode("utf-8")


def fields_for(exe,machine,marker):
    configs=exe.parent/"Machines"/machine/"Configs";configs.mkdir(parents=True,exist_ok=True)
    owners={"BJT":[],"MachineProfile":[]};fields={}
    for control,(scope,name,kind) in field_bindings(machine).items():
        if kind=="toggle":value=True;parameter_type="BoolParameter";fields[control]="toggle:1"
        elif control=="textBoxHeatingOutput":value="62";parameter_type="DoubleParameter";fields[control]="62"
        elif name.startswith("Printhead") and name.endswith("Port"):value="fixture-port";parameter_type="StringParameter";fields[control]=value
        else:value="5";parameter_type="IntParameter" if "Count" in name or "Interval" in name else "DoubleParameter";fields[control]=value
        owners["BJT" if scope=="Printing" else "MachineProfile"].append({"Scope":scope,"Name":name,"Type":parameter_type,"Value":value})
    for owner,values in owners.items():(configs/(owner+".parameters.json")).write_text(json.dumps(values),encoding="utf-8")
    field_path=Path(str(marker)+".fields.json");field_path.write_text(json.dumps(fields),encoding="utf-8")
    return field_path


def exercise(machine,page,minimized,case="normal"):
    name="csv-"+machine+"-"+page+"-"+str(minimized)+"-"+case
    marker=base/name
    for path in base.glob(name+".*"):path.unlink()
    exe=base/"fullroutebin"/"CsvRouteFixture.exe"
    fields=fields_for(exe,machine,marker)
    started=datetime.now().replace(microsecond=0)-timedelta(minutes=5)
    initial_layer=1058 if case=="late" else 0
    monitor=base/"Monitoring"/name
    job=monitor/(started.strftime("%Y%m%d_%H%M%S")+"_1820-layer");job.mkdir(parents=True,exist_ok=True)
    csv_path=job/("LayerDatasheet_"+started.strftime("%Y%m%d_%H%M%S")+".csv")
    def record(index,temp,finish):
        return [str(index+1),started.strftime("%m/%d/%Y %H:%M:%S"),
                (finish-timedelta(seconds=24)).strftime("%m/%d/%Y %H:%M:%S"),
                finish.strftime("%m/%d/%Y %H:%M:%S"),str(temp)]
    csv_path.write_bytes(rows_bytes([list(MonitoringCsvReader.REQUIRED),record(initial_layer,42,started+timedelta(seconds=60))]))
    if case=="newfile":csv_path.unlink()
    log_dir=exe.parent/"Logs";log_dir.mkdir(exist_ok=True)
    log=log_dir/("20261027_"+machine+".log")
    prefix=started.strftime("%Y-%m-%d%p%I%M%S")+"...[BJT - AUTO]: "
    log.write_text(prefix+"AutoPrint started!\n"+prefix+"Layer: "+str(initial_layer)+" -> Current Z = 1.23\n",encoding="utf-8")
    mode="backgroundcsv"+("-"+case if case!="normal" else "")
    fixture=subprocess.Popen([str(exe),str(marker),machine,mode,str(fields)],creationflags=subprocess.CREATE_NO_WINDOW)
    stop=threading.Event();events=queue.Queue();worker=None;phase_times=[];append_times={};received=[]
    def command(**values):
        done=Path(str(marker)+".command-done");done.unlink(missing_ok=True)
        temp=Path(str(marker)+".command.tmp");temp.write_text(json.dumps(values),encoding="utf-8")
        os.replace(temp,Path(str(marker)+".command"))
        until=time.monotonic()+2
        while time.monotonic()<until:
            try:
                if json.loads(done.read_text(encoding="utf-8-sig"))==values:return
            except (OSError,ValueError):pass
            time.sleep(.025)
        raise AssertionError("Own fixture command timed out")
    def make_adapter():
        adapter=WindowsUiAdapter(source_factory=lambda target:MonitoringCsvReader(target,roots=[monitor]))
        actual=adapter._request
        def timed(request,event):
            begin=time.monotonic()
            try:return actual(request,event)
            finally:phase_times.append({"operation":request["operation"],"seconds":round(time.monotonic()-begin,3)})
        adapter._request=timed
        return adapter
    def receive(layer=None,expect_error=False):
        deadline=time.monotonic()+12
        while time.monotonic()<deadline:
            try:kind,payload=events.get(timeout=.25)
            except queue.Empty:continue
            if kind=="error":
                if expect_error:return payload
                raise AssertionError(payload)
            if kind=="snapshot" and payload["snapshot"].layer_index==layer:
                if layer in append_times:received.append(time.monotonic()-append_times.pop(layer))
                return payload
        raise AssertionError("Expected CSV session event was not received")
    try:
        window=Path(str(marker)+".window");until=time.monotonic()+4
        while not window.exists() and time.monotonic()<until:time.sleep(.025)
        pid,hwnd=map(int,window.read_text().split(","))
        target=MachineWindow(machine,pid,hwnd,"Own CSV fixture",str(exe.resolve()))
        before=sequence_persistence.capture(target)
        if case=="connectmin":command(minimized=True)
        options=SessionOptions(target,mode="control")
        def run():
            try:run_ui_session(options,stop,lambda kind,payload:events.put((kind,payload)),make_adapter)
            except Exception as error:events.put(("error",str(error)))
        worker=threading.Thread(target=run,daemon=True);worker.start()
        if case=="dirty":
            receive(expect_error=True)
            assert Path(str(marker)+".saves").read_text()=="0"
            expected_saves=0
        else:
            receive(None if case=="newfile" else initial_layer)
            command(page=page,minimized=minimized)
            layers=(0,1,2) if case=="newfile" else tuple(initial_layer+offset for offset in (1,2,3))
            for order,layer in enumerate(layers,1):
                command(layer=layer+1)
                if case=="statushang" and order==3:command(hangMs=3000)
                raw=rows_bytes([record(layer,42,datetime.now().replace(microsecond=0))])
                if case=="newfile" and order==1:raw=rows_bytes([list(MonitoringCsvReader.REQUIRED)])+raw
                if case=="partial" and layer==1:
                    with csv_path.open("ab") as stream:stream.write(raw[:-3])
                    time.sleep(.6)
                    assert Path(str(marker)+".saves").read_text()=="0"
                    with csv_path.open("ab") as stream:stream.write(raw[-3:])
                else:
                    with csv_path.open("ab") as stream:stream.write(raw)
                append_times[layer]=time.monotonic()
                if order==3 and case in {"savehang","savetardy","edithang","statechange"}:
                    receive(expect_error=True)
                else:
                    payload=receive(layer)
                    assert payload["snapshot"].t_recoating_c==42
                    if order<3:assert Path(str(marker)+".saves").read_text()=="0"
                    if case=="statushang" and order==3:assert payload["snapshot"].machine_state=="unknown",payload
            expected_saves=0 if case in {"edithang","statechange","statushang"} else 1
            if case=="savetardy":
                until=time.monotonic()+7
                while sequence_persistence.capture(target).power!=67 and time.monotonic()<until:time.sleep(.05)
            if case in {"normal","partial","connectmin","late","newfile"}:
                receive(layers[-1]);receive(layers[-1])
                state=json.loads(Path(str(marker)+".background-state").read_text(encoding="utf-8-sig"))
                assert state["page"]==page and state["minimized"]==minimized,state
            current=sequence_persistence.capture(target)
            if case not in {"savehang","edithang","statechange","statushang"}:assert current.power==67,current
            assert current.non_ir==before.non_ir
            assert Path(str(marker)+".saves").read_text()==str(expected_saves)
        assert not Path(str(marker)+".table-access").exists(),"Forbidden table access was recorded"
        assert not any(p["operation"] in {"read","return_process","open_process"} for p in phase_times)
        stop.set();worker.join(6);assert not worker.is_alive()
        result={"machine":machine,"case":case,"page":page,"minimized":minimized,"passed":True,
                "saveCount":expected_saves,"processTableAccesses":0,"nonIrUnchanged":True,
                "completeCsvToSessionSeconds":[round(value,3) for value in received],"uiOperations":phase_times}
        print(json.dumps(result),flush=True);return result
    finally:
        stop.set()
        if worker is not None:worker.join(6)
        if fixture.poll() is None:fixture.kill()
        fixture.wait(2);log.unlink(missing_ok=True)


if __name__=="__main__":
    cases=[(m,p,b) for m in ("M1","S3") for p,b in (("webcam",False),("sequence",False),("webcam",True),("sequence",True))]
    cases += [("M1","webcam",True,case) for case in ("partial","connectmin","savehang","savetardy","edithang","statechange","dirty","late","newfile","statushang")]
    if "--one" in sys.argv:cases=cases[:1]
    results=[exercise(*case) for case in cases]
    (base/"csv-route-results.json").write_text(json.dumps(results,indent=2),encoding="utf-8")
