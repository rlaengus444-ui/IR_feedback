"""Exercise the actual session + Excel controller + native adapter on own UI."""
import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import time

from full_route_probe import base, build_fields, WindowsUiAdapter, MachineWindow, sequence_persistence
from background_route_probe import read_json
from session import SessionOptions, run_ui_session


def exercise(machine):
    marker=base/("background-session-"+machine)
    for path in marker.parent.glob(marker.name+".*"):path.unlink()
    exe=base/"fullroutebin"/"FullRouteFixture.exe"
    fields=build_fields(exe,machine,marker)
    log=exe.parent/"Logs"/("20261027_"+machine+".log")
    log.unlink(missing_ok=True)
    fixture=subprocess.Popen([str(exe),str(marker),machine,"background",str(fields)],creationflags=subprocess.CREATE_NO_WINDOW)
    stop=threading.Event();events=queue.Queue();worker=None
    try:
        deadline=time.monotonic()+4;window=Path(str(marker)+".window")
        while not window.exists() and time.monotonic()<deadline:time.sleep(.025)
        pid,hwnd=map(int,window.read_text().split(","))
        target=MachineWindow(machine,pid,hwnd,"Own synthetic fixture",str(exe.resolve()))
        options=SessionOptions(target,mode="control")
        def run():
            try:run_ui_session(options,stop,lambda kind,payload:events.put((kind,payload)),WindowsUiAdapter)
            except Exception as error:events.put(("error",str(error)))
        worker=threading.Thread(target=run,daemon=True);worker.start()
        def receive(layer):
            deadline=time.monotonic()+10
            while time.monotonic()<deadline:
                try:kind,payload=events.get(timeout=.25)
                except queue.Empty:continue
                if kind=="error":raise AssertionError(payload)
                if kind=="snapshot" and payload["snapshot"].layer_index==layer:return payload
            raise AssertionError("Actual session did not receive the expected layer "+str(layer))
        receive(0)
        before=sequence_persistence.capture(target)
        steps=[]
        for layer,page in ((1,"webcam"),(2,"sequence"),(3,"webcam")):
            done=Path(str(marker)+".command-done");done.unlink(missing_ok=True)
            temp=Path(str(marker)+".command.tmp")
            temp.write_text(json.dumps({"page":page,"minimized":True,"layer":layer,"temperature":42}),encoding="utf-8")
            os.replace(temp,Path(str(marker)+".command"))
            payload=receive(layer)
            count=int(Path(str(marker)+".saves").read_text())
            assert count==(1 if layer==3 else 0),(layer,count,payload)
            state=read_json(Path(str(marker)+".background-state"))
            assert state["page"]==page and state["minimized"],state
            steps.append({"zeroBasedLayer":layer,"page":page,"minimized":True,"saveCount":count,"status":payload["status"]})
        persisted=sequence_persistence.capture(target)
        assert persisted.power==67 and persisted.non_ir==before.non_ir,persisted
        # Duplicate polling must not run a second Save or advance the algorithm.
        receive(3);receive(3)
        assert Path(str(marker)+".saves").read_text()=="1"
        stop.set();worker.join(6)
        assert not worker.is_alive(),"Session disconnect did not finish"
        result={"machine":machine,"passed":True,"actualSessionAndExcelAlgorithm":True,"firstTwoNewRowsHeld":True,
                "thirdNewRowSavedOnce":True,"duplicatePollSaveCount":1,"powerPercent":67,"nonIrUnchanged":True,
                "disconnectCompleted":True,"steps":steps}
        print(json.dumps(result,ensure_ascii=False),flush=True)
        return result
    finally:
        stop.set()
        if worker is not None:worker.join(6)
        if fixture.poll() is None:fixture.kill()
        fixture.wait(timeout=2);log.unlink(missing_ok=True)


if __name__=="__main__":
    results=[exercise(machine) for machine in ("M1","S3")]
    (base/"background-session-results.json").write_text(json.dumps(results,indent=2,ensure_ascii=False),encoding="utf-8")
