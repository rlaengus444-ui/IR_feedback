"""Product adapter against own WinForms fixture with a frozen hidden UI cache.

Commands affect only a fixture whose PID/HWND were written by this run. The
fixture's source temperature changes independently from its stopped UI timer.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

from full_route_probe import base, build_fields, WindowsUiAdapter, MachineWindow, sequence_persistence
from adapter_contracts import AdapterError, ControlUnavailable


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def exercise(machine, page, minimized, case="background"):
    marker=base/("background-"+machine+"-"+page+"-"+str(minimized)+"-"+case)
    for path in marker.parent.glob(marker.name+".*"):
        path.unlink()
    exe=base/"fullroutebin"/"FullRouteFixture.exe"
    field_path=build_fields(exe,machine,marker)
    log=exe.parent/"Logs"/("20261027_"+machine+".log")
    log.unlink(missing_ok=True)
    fixture=subprocess.Popen([str(exe),str(marker),machine,case,str(field_path)],creationflags=subprocess.CREATE_NO_WINDOW)
    adapter=WindowsUiAdapter()
    event=threading.Event()
    started=time.perf_counter()
    phases=[]
    real_request=adapter._request
    def timed(request,stop):
        begin=time.perf_counter()
        try:return real_request(request,stop)
        finally:phases.append({"operation":request["operation"],"seconds":round(time.perf_counter()-begin,3)})
    adapter._request=timed

    def command(**values):
        done=Path(str(marker)+".command-done");done.unlink(missing_ok=True)
        temp=Path(str(marker)+".command.tmp")
        temp.write_text(json.dumps(values),encoding="utf-8")
        os.replace(temp,Path(str(marker)+".command"))
        deadline=time.monotonic()+2
        while time.monotonic()<deadline:
            if read_json(done)==values:return
            time.sleep(.025)
        raise AssertionError("Own fixture did not acknowledge its test command")

    def view():
        deadline=time.monotonic()+2
        while time.monotonic()<deadline:
            state=read_json(Path(str(marker)+".background-state"))
            if state and state["page"]==page and state["minimized"]==minimized:return state
            time.sleep(.025)
        raise AssertionError("The fixture page/minimization changed: "+str(state))

    try:
        deadline=time.monotonic()+4
        window=Path(str(marker)+".window")
        while not window.exists() and time.monotonic()<deadline:time.sleep(.025)
        pid,hwnd=map(int,window.read_text().split(","))
        target=MachineWindow(machine,pid,hwnd,"BinderJet synthetic fixture",str(exe.resolve()))
        if case=="backgroundconnectmin":command(minimized=True)
        adapter.connect(target,event)
        adapter.prepare_mode("control",event)
        first=adapter.read_snapshot(event)
        if case=="backgroundlate":
            while first.observation_key is None and time.monotonic()<deadline+15:
                time.sleep(1.01);first=adapter.read_snapshot(event)
        assert first.t_recoating_c==42,first
        baseline=sequence_persistence.capture(target)
        before_navigation=Path(str(marker)+".sequence-clicks").read_text()
        phase_start=len(phases)
        command(page=page,minimized=minimized,temperature=53)
        state=view()
        refresh_before=state["refreshCount"]
        # The fixture suppresses its table timer both when hidden and minimized.
        # Its heartbeat continues; this delay cannot update the table cache.
        if page!="process" or minimized:
            time.sleep(1.1)
            assert view()["refreshCount"]==refresh_before,"Hidden cache timer was not actually stopped"
        adapter._last_read=0
        if case=="nostroll":
            try:adapter.read_snapshot(event)
            except AdapterError as error:
                assert "cached temperature was rejected" in str(error),str(error)
            else:raise AssertionError("A non-scrollable hidden cache must not be accepted as fresh")
            assert Path(str(marker)+".saves").read_text()=="0"
            result={"machine":machine,"case":case,"page":page,"minimized":minimized,"passed":True,"saveCount":0,"staleCacheRejected":True}
        else:
            fresh=adapter.read_snapshot(event)
            assert fresh.t_recoating_c==53 and fresh.observation_key!=first.observation_key,fresh
            if page!="process" or minimized:
                deadline=time.monotonic()+1
                while view()["refreshCount"]<refresh_before+2 and time.monotonic()<deadline:time.sleep(.025)
                assert view()["refreshCount"]>=refresh_before+2,"Existing Scroll refresh did not run"
            next_layer=(first.layer_index or 0)+1
            command(layer=next_layer,temperature=43)
            adapter._last_read=0
            fresh=adapter.read_snapshot(event)
            assert fresh.layer_index==next_layer and fresh.t_recoating_c==43,fresh
            expected_saves=1
            if case=="backgrounddirty":
                command(dirty=True)
                try:adapter.apply_ir_power(1,67,fresh.observation_key,event)
                except ControlUnavailable:pass
                else:raise AssertionError("A hidden non-IR pending edit must block Save")
                expected_saves=0
            elif case=="backgroundadvance":
                def advance_between_verify_and_edit(request,stop):
                    response=timed(request,stop)
                    if request["operation"]=="verify_control":command(layer=next_layer+1,temperature=44)
                    return response
                adapter._request=advance_between_verify_and_edit
                try:adapter.apply_ir_power(1,67,fresh.observation_key,event)
                except AdapterError:pass
                else:raise AssertionError("A newer row must block the old decision before edit")
                expected_saves=0
            else:
                adapter.apply_ir_power(1,67,fresh.observation_key,event)
            persisted=sequence_persistence.capture(target)
            assert persisted.power==(67 if expected_saves else 62) and persisted.non_ir==baseline.non_ir,persisted
            assert Path(str(marker)+".saves").read_text()==str(expected_saves)
            if not expected_saves:
                fields=read_json(Path(str(marker)+".fields-state"))
                assert fields["textBoxHeatingOutput"]=="62",fields
            view()
            assert Path(str(marker)+".sequence-clicks").read_text()==before_navigation,"Background feedback changed tabs"
            assert not any(p["operation"] in {"enter_sequence","return_process","metadata","prepare_control"} for p in phases[phase_start:]),phases
            result={"machine":machine,"case":case,"page":page,"minimized":minimized,"passed":True,
                    "saveCount":expected_saves,"frozenCacheRefreshed":True,"sameRowNewTemperatureRead":53,
                    "newLayerTemperatureRead":43,"pagePreserved":True,"minimizedPreserved":True,"nonIrUnchanged":True,
                    "elapsedSeconds":round(time.perf_counter()-started,3),"uiOperations":phases}
        print(json.dumps(result),flush=True)
        return result
    finally:
        adapter.disconnect()
        if fixture.poll() is None:fixture.kill()
        fixture.wait(timeout=2)
        log.unlink(missing_ok=True)


if __name__=="__main__":
    cases=[(machine,page,minimized) for machine in ("M1","S3") for page,minimized in
           (("webcam",False),("sequence",False),("process",True),("webcam",True),("sequence",True))]
    cases += [("M1","webcam",True,"backgroundconnectmin"),("M1","webcam",True,"backgroundlate"),
              ("M1","webcam",True,"backgrounddirty"),("M1","webcam",True,"backgroundadvance"),
              ("M1","webcam",True,"nostroll")]
    if "--one" in sys.argv:cases=cases[:1]
    if "--extras" in sys.argv:cases=cases[10:]
    results=[exercise(*case) for case in cases]
    (base/"background-results.json").write_text(json.dumps(results,indent=2),encoding="utf-8")
