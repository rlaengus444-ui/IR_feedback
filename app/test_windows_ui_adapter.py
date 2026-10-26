"""CSV acquisition and native Sequence orchestration; all data are synthetic."""
import csv
from datetime import datetime, timedelta
import io
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from adapter_contracts import AdapterError, AmbiguousWriteError, ControlUnavailable, MachineWindow
from csv_source import MonitoringCsvReader
from windows_ui_adapter import WindowsUiAdapter, _HELPER


def csv_bytes(rows):
    text=io.StringIO(newline="")
    csv.writer(text,lineterminator="\r\n").writerows(rows)
    return text.getvalue().encode("utf-8")


class AdapterTests(unittest.TestCase):
    def fixture(self,directory,mode="control"):
        root=Path(directory)
        target=MachineWindow("M1",1234,5678,"Own synthetic target",str(root/"Operator.exe"))
        configs=root/"Machines/M1/Configs";configs.mkdir(parents=True)
        path=configs/"BJT.parameters.json"
        values=[{"Scope":"Printing","Name":"IRHeatingOutputPct","Type":"DoubleParameter","Value":"62"},
                {"Scope":"Printing","Name":"OtherSpeed","Type":"DoubleParameter","Value":"5"}]
        path.write_text(json.dumps(values),encoding="utf-8")
        started=datetime.now().replace(microsecond=0)-timedelta(minutes=5)
        logs=root/"Logs";logs.mkdir()
        log=logs/"20261027_M1.log"
        def log_line(stamp,text):
            return stamp.strftime("%Y-%m-%d%p%I%M%S")+"...[BJT - AUTO]: "+text+"\n"
        log.write_text(log_line(started,"AutoPrint started!")+log_line(started,"Layer: 0 -> Current Z = 1.2"),encoding="utf-8")
        monitoring=root/"Monitoring"
        job=monitoring/(started.strftime("%Y%m%d_%H%M%S")+"_1820-layer");job.mkdir(parents=True)
        source=job/("LayerDatasheet_"+started.strftime("%Y%m%d_%H%M%S")+".csv")
        header=list(MonitoringCsvReader.REQUIRED)
        def row(index,temperature,finish):
            return [str(index+1),started.strftime("%m/%d/%Y %H:%M:%S"),
                    (finish-timedelta(seconds=24)).strftime("%m/%d/%Y %H:%M:%S"),
                    finish.strftime("%m/%d/%Y %H:%M:%S"),str(temperature)]
        source.write_bytes(csv_bytes([header,row(0,42,started+timedelta(seconds=60))]))
        state={"power":"62","speed":"5","saves":0,"page":"webcam","state":"AUTO",
               "skip_persist":False,"fail_after_edit":False,"hook":None}
        calls=[]
        def runner(request,stop):
            calls.append(request)
            fields={"textBoxHeatingOutput":state["power"],"textBoxSpeed":state["speed"],"checkBoxHeat":"toggle:1"}
            op=request["operation"]
            if op=="discover":
                return {"success":True,"windows":[dict(machine="M1",pid=1234,hwnd=5678,title="Synthetic",image_path=target.image_path)]}
            if op=="enter_sequence":
                state["page"]="sequence"
                return {"success":True,"entered":True}
            if op=="metadata":
                return {"success":True,"records":[{"id":"textBoxHeatingOutput","visible":True},{"id":"btnSave","visible":True}]}
            if op in {"prepare_control","verify_control"}:
                if state["hook"] and op=="verify_control":state["hook"](request)
                return {"success":True,"fields":fields,"setting":state["power"],"handles":{"state":99}}
            if op=="apply":
                Path(request["edit_marker"]).write_text("edit")
                if state["fail_after_edit"]:raise AdapterError("Own fake edit interrupted")
                state["power"]=request["power"]
                Path(request["save_marker"]).write_text("save")
                state["saves"]+=1
                if not state["skip_persist"]:
                    values[0]["Value"]=state["power"];path.write_text(json.dumps(values),encoding="utf-8")
                return {"success":True,"save_invoked":True,"setting":state["power"]}
            if op=="read_status" and state.get("status_timeout"):
                return {"success":False,"error":"The selected control did not answer WM_GETTEXT within 250 ms."}
            return {"success":True,"machine":"M1","state":state["state"]}
        audit=patch("windows_ui_adapter.assert_fields_match_persisted")
        mocked=audit.start();self.addCleanup(audit.stop)
        adapter=WindowsUiAdapter(runner=runner,source_factory=lambda t:MonitoringCsvReader(t,roots=[monitoring]))
        adapter.connect(target,threading.Event())
        adapter.prepare_mode(mode,threading.Event())
        baseline=adapter.read_snapshot(threading.Event())
        def append(index=1,temperature=43,age=0,partial=False):
            now=datetime.now().replace(microsecond=0)-timedelta(seconds=age)
            encoded=csv_bytes([row(index,temperature,now)])
            with source.open("ab") as stream:stream.write(encoded[:-3] if partial else encoded)
            with log.open("a",encoding="utf-8") as stream:stream.write(log_line(datetime.now(),"Layer: "+str(index+1)+" -> Current Z = 1.1"))
            adapter._last_read=0
            return adapter.read_snapshot(threading.Event())
        return adapter,state,calls,path,values,append,baseline,source,mocked

    def test_csv_monitor_reads_without_ui_provider_requests(self):
        with tempfile.TemporaryDirectory() as d:
            a,state,calls,path,values,append,baseline,source,audit=self.fixture(d,"monitor")
            self.assertEqual((0,42,62),(baseline.layer_index,baseline.t_recoating_c,baseline.heater1_setting_percent))
            snapshot=append()
            self.assertEqual((1,43),(snapshot.layer_index,snapshot.t_recoating_c))
            self.assertEqual(["validate"],[c["operation"] for c in calls])
            self.assertFalse(snapshot.control_available)

    def test_control_never_opens_reads_or_scrolls_process_table(self):
        with tempfile.TemporaryDirectory() as d:
            a,state,calls,path,values,append,baseline,source,audit=self.fixture(d)
            snapshot=append()
            state["page"]="webcam"
            a.apply_ir_power(1,67,snapshot.observation_key,threading.Event())
            self.assertEqual("webcam",state["page"])
            self.assertFalse(any(c["operation"] in {"read","return_process","open_process","follow_row"} for c in calls))
            self.assertEqual(1,state["saves"])

    def test_initial_history_cannot_be_saved_as_a_new_measurement(self):
        with tempfile.TemporaryDirectory() as d:
            a,state,calls,path,values,append,baseline,source,audit=self.fixture(d)
            with self.assertRaises(ControlUnavailable):a.apply_ir_power(1,67,baseline.observation_key,threading.Event())
            self.assertEqual(0,state["saves"])

    def test_new_csv_record_saves_once_and_verifies_persistence(self):
        with tempfile.TemporaryDirectory() as d:
            a,state,calls,path,values,append,baseline,source,audit=self.fixture(d)
            snapshot=append()
            self.assertTrue(snapshot.observation_is_new)
            a.apply_ir_power(1,67,snapshot.observation_key,threading.Event())
            self.assertEqual("67",json.loads(path.read_text())[0]["Value"])
            self.assertEqual("5",json.loads(path.read_text())[1]["Value"])
            again=a.read_snapshot(threading.Event())
            with self.assertRaises(ControlUnavailable):a.apply_ir_power(1,67,again.observation_key,threading.Event())
            self.assertEqual(1,state["saves"])

    def test_fractional_ir_setting_is_not_rounded_to_integer(self):
        with tempfile.TemporaryDirectory() as d:
            a,state,calls,path,values,append,baseline,source,audit=self.fixture(d)
            snapshot=append()
            a.apply_ir_power(1,67.5,snapshot.observation_key,threading.Event())
            self.assertEqual("67.5",json.loads(path.read_text())[0]["Value"])

    def test_partial_csv_record_cannot_trigger_save(self):
        with tempfile.TemporaryDirectory() as d:
            a,state,calls,path,values,append,baseline,source,audit=self.fixture(d)
            snapshot=append(partial=True)
            self.assertIsNone(snapshot.observation_key)
            with self.assertRaises(ControlUnavailable):a.apply_ir_power(1,67,snapshot.observation_key,threading.Event())
            self.assertEqual(0,state["saves"])

    def test_stale_completed_record_is_not_applied(self):
        with tempfile.TemporaryDirectory() as d:
            a,state,calls,path,values,append,baseline,source,audit=self.fixture(d)
            snapshot=append(age=30)
            self.assertIsNone(snapshot.observation_key)
            self.assertFalse(snapshot.observation_is_new)
            self.assertEqual(0,state["saves"])

    def test_newer_record_between_preflight_and_edit_blocks_save(self):
        with tempfile.TemporaryDirectory() as d:
            a,state,calls,path,values,append,baseline,source,audit=self.fixture(d)
            snapshot=append()
            state["hook"]=lambda request:append(index=2,temperature=44)
            with self.assertRaises(ControlUnavailable):a.apply_ir_power(1,67,snapshot.observation_key,threading.Event())
            self.assertFalse(any(c["operation"]=="apply" for c in calls))
            self.assertEqual(0,state["saves"])

    def test_unrelated_pending_sequence_edit_blocks_before_ir_edit(self):
        with tempfile.TemporaryDirectory() as d:
            a,state,calls,path,values,append,baseline,source,audit=self.fixture(d)
            snapshot=append();state["speed"]="999"
            with self.assertRaises(ControlUnavailable):a.apply_ir_power(1,67,snapshot.observation_key,threading.Event())
            self.assertEqual(0,state["saves"])
            self.assertFalse(any(c["operation"]=="apply" for c in calls))

    def test_saved_other_parameter_change_blocks_before_edit(self):
        with tempfile.TemporaryDirectory() as d:
            a,state,calls,path,values,append,baseline,source,audit=self.fixture(d)
            snapshot=append();values[1]["Value"]="6";path.write_text(json.dumps(values))
            with self.assertRaises(ControlUnavailable):a.apply_ir_power(1,67,snapshot.observation_key,threading.Event())
            self.assertEqual(0,state["saves"])

    def test_disconnect_during_preflight_cannot_send_late_apply(self):
        with tempfile.TemporaryDirectory() as d:
            a,state,calls,path,values,append,baseline,source,audit=self.fixture(d)
            snapshot=append();state["hook"]=lambda request:a.disconnect()
            with self.assertRaises(AdapterError):a.apply_ir_power(1,67,snapshot.observation_key,threading.Event())
            self.assertFalse(any(c["operation"]=="apply" for c in calls))

    def test_ambiguous_edit_never_retries_or_undoes(self):
        with tempfile.TemporaryDirectory() as d:
            a,state,calls,path,values,append,baseline,source,audit=self.fixture(d)
            snapshot=append();state["fail_after_edit"]=True
            with self.assertRaises(AmbiguousWriteError):a.apply_ir_power(1,67,snapshot.observation_key,threading.Event())
            with self.assertRaises(ControlUnavailable):a.apply_ir_power(1,67,snapshot.observation_key,threading.Event())
            self.assertEqual(1,sum(c["operation"]=="apply" for c in calls))
            self.assertEqual(0,state["saves"])

    def test_text_readback_without_saved_file_does_not_claim_success(self):
        with tempfile.TemporaryDirectory() as d:
            a,state,calls,path,values,append,baseline,source,audit=self.fixture(d)
            snapshot=append();state["skip_persist"]=True
            with patch("windows_ui_adapter.sequence_persistence.verify_after",side_effect=RuntimeError("not saved")):
                with self.assertRaises(AmbiguousWriteError):a.apply_ir_power(1,67,snapshot.observation_key,threading.Event())
            self.assertEqual("62",json.loads(path.read_text())[0]["Value"])
            self.assertEqual(1,state["saves"])

    def test_pause_state_is_read_without_table_access(self):
        with tempfile.TemporaryDirectory() as d:
            a,state,calls,path,values,append,baseline,source,audit=self.fixture(d)
            state["state"]="PAUSED";a._status_at=0
            snapshot=append()
            self.assertEqual("PAUSED",snapshot.machine_state)
            self.assertFalse(any(c["operation"]=="read" for c in calls))

    def test_preview_performs_no_sequence_or_save_requests(self):
        with tempfile.TemporaryDirectory() as d:
            a,state,calls,path,values,append,baseline,source,audit=self.fixture(d,"preview")
            append()
            self.assertEqual(["validate"],[c["operation"] for c in calls])

    def test_original_ui_read_timeout_keeps_csv_but_suspends_control(self):
        with tempfile.TemporaryDirectory() as d:
            a,state,calls,path,values,append,baseline,source,audit=self.fixture(d)
            state["status_timeout"]=True;a._status_at=0
            result=append()
            self.assertEqual(43,result.t_recoating_c)
            self.assertEqual("unknown",result.machine_state)
            self.assertIn("CSV 조회만",result.details)
            self.assertEqual(0,state["saves"])

    def test_fast_poll_returns_cached_snapshot_without_transport(self):
        with tempfile.TemporaryDirectory() as d:
            a,state,calls,path,values,append,baseline,source,audit=self.fixture(d,"monitor")
            count=len(calls)
            for unused in range(1000):self.assertIs(baseline,a.read_snapshot(threading.Event()))
            self.assertEqual(count,len(calls))

    def test_cancelled_connect_is_resource_only(self):
        cancelled=threading.Event();cancelled.set();calls=[]
        a=WindowsUiAdapter(runner=lambda req,stop:calls.append(req))
        with self.assertRaises(AdapterError):a.connect(MachineWindow("M1",1,2,"x","x"),cancelled)
        self.assertEqual([],calls)

    def test_hung_helper_is_bounded_and_only_own_helper_is_killed(self):
        class Hung:
            killed=False
            def poll(self):return -9 if self.killed else None
            def communicate(self,payload=None,timeout=None):
                if self.killed:return "",""
                time.sleep(timeout);raise subprocess.TimeoutExpired("own fake helper",timeout)
            def kill(self):self.killed=True
        child=Hung();a=WindowsUiAdapter(helper_timeout=.1);started=time.monotonic()
        with patch("windows_ui_adapter.subprocess.Popen",return_value=child):
            with self.assertRaises(AdapterError):a.discover()
        self.assertTrue(child.killed)
        self.assertLess(time.monotonic()-started,.5)

    def test_production_transport_has_no_table_or_scroll_entry_point(self):
        source=Path(__file__).with_name("native_helper.ps1").read_text(encoding="utf-8-sig")
        for forbidden in ("BinderJetAccessibleGrid","BinderJetQueuedGridScroll","FindGrid","return_process","AutomationElement","InvokePattern","BoundingRectangle"):
            self.assertNotIn(forbidden,source)
        binary=Path(__file__).with_name("UiaBridge.dll").read_bytes()
        self.assertNotIn(b"BinderJetAccessibleGrid",binary)
        self.assertNotIn(b"BinderJetQueuedGridScroll",binary)
        self.assertIn("AssertCsvFile",source)
        self.assertIn("[BinderJetNativeFields]::QueueSave",source)


if __name__=="__main__":unittest.main()
