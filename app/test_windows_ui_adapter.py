import subprocess
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from adapter_contracts import AdapterError, AmbiguousWriteError, ControlUnavailable, MachineWindow
from windows_ui_adapter import WindowsUiAdapter, _HELPER, _log_layer_hint


TARGET = MachineWindow("M1", 1234, 5678, "BinderJet", "C:/test/Operator.exe")


class WindowsUiAdapterTests(unittest.TestCase):
    def make_adapter(self, rows=None):
        calls = []
        def runner(request, stop):
            calls.append(request)
            if request["operation"] == "discover":
                return {"success": True, "windows": [{"machine": "M1", "pid": 1234, "hwnd": 5678, "title": "BinderJet", "image_path": "test.exe"}]}
            return {"success": True, "machine": "M1", "state": "AUTO", "setting": "62", "rows": rows or [], "visible_grid": bool(rows)}
        return WindowsUiAdapter(runner=runner), calls

    def test_discovery_connect_and_valid_visible_recoating_data(self):
        adapter, calls = self.make_adapter([
            {"row_index": 7, "temperature": "42.5", "start": "2026-10-27 08:30:00"},
            {"row_index": 8, "temperature": "0", "start": "0001-01-01 00:00:00"},
        ])
        self.assertEqual("M1", adapter.discover()[0].machine)
        adapter.connect(TARGET, threading.Event())
        state = adapter.read_snapshot(threading.Event())
        self.assertEqual((7, 42.5, 62, 62, "AUTO"), (state.layer_index, state.t_recoating_c, state.heater1_setting_percent, state.heater2_setting_percent, state.machine_state))
        self.assertTrue(state.observation_key)
        self.assertFalse(state.control_available)
        self.assertEqual(["discover", "validate", "read"], [call["operation"] for call in calls])

    def test_fast_poll_uses_memory_and_never_touches_ui_provider_again(self):
        adapter, calls = self.make_adapter()
        adapter.connect(TARGET, threading.Event())
        first = adapter.read_snapshot(threading.Event())
        for _ in range(1000):
            self.assertIs(first, adapter.read_snapshot(threading.Event()))
        self.assertEqual(2, len(calls))
        self.assertIsNone(first.t_recoating_c)
        self.assertIn("Layer", first.details)

    def test_control_is_explicitly_unavailable_and_cannot_create_any_write_request(self):
        adapter, calls = self.make_adapter()
        adapter.connect(TARGET, threading.Event())
        with self.assertRaises(ControlUnavailable):
            adapter.apply_ir_power(1, 50, "observed", threading.Event())
        self.assertEqual(["validate"], [call["operation"] for call in calls])
        for unsafe in ("WriteProcessMemory", "SendInput", "WM_SETTEXT", "SetFocus("):
            self.assertNotIn(unsafe, _HELPER)

    def test_native_dispatcher_cannot_materialize_a_form_or_grid_automation_tree(self):
        # Form/grid UIA access reproduced a host UI-thread Bounds recursion.
        # Keep native transport free of those APIs; the one metadata operation
        # delegates to the separately bounded leaf-ID-only collector.
        source = Path(__file__).with_name("native_helper.ps1").read_text(encoding="utf-8-sig")
        for unsafe in ("AutomationElement", "TreeWalker", "IsOffscreen", "BoundingRectangle", "ValuePattern", "InvokePattern"):
            self.assertNotIn(unsafe, source)
        self.assertIn("[BinderJetLeafMetadata]::Collect", source)

    def test_unknown_machine_and_invalid_or_default_rows_are_not_claimed_as_measurements(self):
        adapter, _ = self.make_adapter([None, [], {"row_index": 1, "temperature": "NaN", "start": "now"}, {"row_index": 2, "temperature": "0", "start": "now"}])
        unknown = MachineWindow("unknown", 1234, 5678, "BinderJet", "operator.exe")
        adapter.connect(unknown, threading.Event())
        state = adapter.read_snapshot(threading.Event())
        self.assertIsNone(state.observation_key)
        self.assertIsNone(state.t_recoating_c)
        self.assertIn("M1/S3", state.control_unavailable_reason)

    def test_disconnect_and_cancellation_are_resource_only(self):
        adapter, calls = self.make_adapter()
        adapter.connect(TARGET, threading.Event())
        adapter.disconnect()
        with self.assertRaises(AdapterError):
            adapter.read_snapshot(threading.Event())
        cancelled = threading.Event()
        cancelled.set()
        with self.assertRaises(AdapterError):
            adapter.connect(TARGET, cancelled)
        self.assertEqual(1, len(calls))

    def test_hung_ui_provider_is_bounded_and_only_own_helper_is_killed(self):
        class HungHelper:
            def __init__(self): self.killed = False
            def poll(self): return -9 if self.killed else None
            def communicate(self, payload=None, timeout=None):
                if self.killed: return "", ""
                time.sleep(timeout)
                raise subprocess.TimeoutExpired("test UIA helper", timeout)
            def kill(self): self.killed = True
        helper = HungHelper()
        adapter = WindowsUiAdapter(helper_timeout=0.1)
        started = time.monotonic()
        with patch("windows_ui_adapter.subprocess.Popen", return_value=helper):
            with self.assertRaises(AdapterError):
                adapter.discover()
        self.assertTrue(helper.killed)
        self.assertLess(time.monotonic() - started, 0.5)

    def control_fixture(self, directory, audit_error=None, initial_page="process"):
        # Field mapping/type coverage is tested against all real bindings in
        # test_sequence_field_audit; this fixture isolates adapter orchestration.
        audit_patch = patch("windows_ui_adapter.assert_fields_match_persisted", side_effect=audit_error)
        audit = audit_patch.start()
        self.addCleanup(audit_patch.stop)
        target = MachineWindow("M1", 1234, 5678, "BinderJet", str(Path(directory) / "Operator.exe"))
        config = Path(directory) / "Machines/M1/Configs/BJT.parameters.json"
        config.parent.mkdir(parents=True)
        values = [{"Scope": "Printing", "Name": "IRHeatingOutputPct", "Type": "DoubleParameter", "Value": "62"},
                  {"Scope": "Printing", "Name": "OtherSpeed", "Type": "DoubleParameter", "Value": "5"}]
        config.write_text(json.dumps(values))
        state = {"power": "62", "speed": "5", "saves": 0, "fail_after_edit": False, "skip_persist": False, "page": initial_page}
        calls = []
        def runner(request, stop):
            calls.append(request)
            fields = {"textBoxHeatingOutput": state["power"], "textBoxSpeed": state["speed"], "checkBoxHeat": "toggle:1"}
            if request["operation"] == "enter_sequence":
                entered = state["page"] == "process" or bool(request.get("force_sequence"))
                if entered:
                    state["page"] = "sequence"
                return {"success": True, "entered": entered}
            if request["operation"] == "metadata":
                return {"success": True, "records": ([{"id": "textBoxHeatingOutput", "visible": True}, {"id": "btnSave", "visible": True}]
                                                       if state["page"] == "sequence" else [])}
            if request["operation"] == "return_process":
                state["page"] = "process"
                return {"success": True}
            if request["operation"] == "prepare_control":
                return {"success": True, "setting": state["power"], "fields": fields}
            if request["operation"] == "apply":
                if request["baseline"]["textBoxSpeed"] != state["speed"]:
                    raise AdapterError("Another Sequence setting changed")
                Path(request["edit_marker"]).write_text("started")
                if state["fail_after_edit"]:
                    raise AdapterError("provider stopped during edit")
                Path(request["save_marker"]).write_text("started")
                state["power"] = str(request["power"])
                state["saves"] += 1
                if not state["skip_persist"]:
                    values[0]["Value"] = state["power"]
                    config.write_text(json.dumps(values))
                return {"success": True, "save_invoked": True, "setting": state["power"], "fields": dict(fields, textBoxHeatingOutput=state["power"])}
            return {"success": True, "machine": "M1", "state": "AUTO", "setting": state["power"],
                    "rows": [] if state.get("empty") else [{"row_index": state.get("row", 0), "temperature": "42", "start": "2026-10-27 08:30:00"}], "visible_grid": True,
                    "progress": "Layer 1 / 1000 (1%)"}
        adapter = WindowsUiAdapter(runner=runner)
        adapter._test_audit = audit
        adapter.connect(target, threading.Event())
        adapter.prepare_mode("control", threading.Event())
        snapshot = adapter.read_snapshot(threading.Event())
        return adapter, state, calls, config, values, snapshot

    def test_explicit_control_saves_once_and_requires_persisted_ir_readback(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter, state, calls, config, values, snapshot = self.control_fixture(directory)
            self.assertTrue(snapshot.control_available)
            self.assertIsNotNone(snapshot.run_key)
            adapter.apply_ir_power(1, 67, snapshot.observation_key, threading.Event())
            self.assertEqual(1, state["saves"])
            self.assertEqual("67", json.loads(config.read_text())[0]["Value"])
            self.assertEqual("5", json.loads(config.read_text())[1]["Value"])
            self.assertEqual(67, adapter.read_snapshot(threading.Event()).heater1_setting_percent)
            self.assertEqual(2, adapter._test_audit.call_count)

    def test_existing_unsaved_fields_block_control_admission(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ControlUnavailable):
                self.control_fixture(directory, audit_error=ValueError("textBoxLayerThickness differs from persisted value"))

    def test_initial_other_page_enters_sequence_once_but_existing_sequence_is_not_reentered(self):
        for initial_page in ("webcam", "sequence"):
            with self.subTest(initial_page=initial_page), tempfile.TemporaryDirectory() as directory:
                adapter, state, calls, config, values, snapshot = self.control_fixture(directory, initial_page=initial_page)
                forced = [call for call in calls if call["operation"] == "enter_sequence" and call.get("force_sequence")]
                self.assertEqual(1 if initial_page == "webcam" else 0, len(forced))
                self.assertEqual(0, state["saves"])
                self.assertTrue(snapshot.control_available)

    def test_disconnect_during_metadata_prevents_a_late_edit_or_save(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter, state, calls, config, values, snapshot = self.control_fixture(directory)
            original = adapter._runner
            def disconnecting(request, stop):
                result = original(request, stop)
                if request["operation"] == "metadata":
                    adapter.disconnect()
                return result
            adapter._runner = disconnecting
            with self.assertRaises(AdapterError):
                adapter.apply_ir_power(1, 67, snapshot.observation_key, threading.Event())
            self.assertFalse(any(call["operation"] == "apply" for call in calls))
            self.assertEqual(0, state["saves"])
            self.assertEqual("62", json.loads(config.read_text())[0]["Value"])

    def test_process_return_failure_after_verified_save_is_not_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter, state, calls, config, values, snapshot = self.control_fixture(directory)
            original = adapter._runner
            def failed_return(request, stop):
                if request["operation"] == "return_process":
                    raise AdapterError("Process return acknowledgement failed")
                return original(request, stop)
            adapter._runner = failed_return
            with self.assertRaises(AmbiguousWriteError):
                adapter.apply_ir_power(1, 67, snapshot.observation_key, threading.Event())
            with self.assertRaises(ControlUnavailable):
                adapter.apply_ir_power(1, 67, snapshot.observation_key, threading.Event())
            self.assertEqual(1, state["saves"])
            self.assertEqual("67", json.loads(config.read_text())[0]["Value"])

    def test_repeated_field_audit_failure_blocks_before_any_input_edit(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter, state, calls, config, values, snapshot = self.control_fixture(directory)
            adapter._test_audit.side_effect = ValueError("staged field differs")
            with self.assertRaises(ControlUnavailable):
                adapter.apply_ir_power(1, 67, snapshot.observation_key, threading.Event())
            self.assertFalse(any(call["operation"] == "apply" for call in calls))
            self.assertEqual(0, state["saves"])

    def test_fractional_save_preserves_requested_setting_without_integer_rounding(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter, state, calls, config, values, snapshot = self.control_fixture(directory)
            adapter.apply_ir_power(1, 67.5, snapshot.observation_key, threading.Event())
            self.assertEqual("67.5", json.loads(config.read_text())[0]["Value"])
            self.assertEqual("67.5", next(call for call in calls if call["operation"] == "apply")["power"])
            self.assertEqual(67.5, adapter.read_snapshot(threading.Event()).heater1_setting_percent)

    def test_transient_empty_rows_preserve_capability_but_cannot_trigger_a_save(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter, state, calls, config, values, snapshot = self.control_fixture(directory)
            state["empty"] = True
            adapter._last_read = 0
            waiting = adapter.read_snapshot(threading.Event())
            self.assertTrue(waiting.control_available)
            self.assertIsNone(waiting.observation_key)
            with self.assertRaises(ControlUnavailable):
                adapter.apply_ir_power(1, 67, snapshot.observation_key, threading.Event())
            self.assertEqual(0, state["saves"])

    def test_previous_job_future_row_is_not_trusted_against_layer_zero_hint(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter, state, calls, config, values, snapshot = self.control_fixture(directory)
            state["row"] = 8
            adapter._last_read = 0
            waiting = adapter.read_snapshot(threading.Event())
            self.assertTrue(waiting.control_available)
            self.assertIsNone(waiting.observation_key)
            with self.assertRaises(ControlUnavailable):
                adapter.apply_ir_power(1, 67, snapshot.observation_key, threading.Event())
            self.assertEqual(0, state["saves"])

    def test_stale_observation_or_changed_other_settings_prevents_save(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter, state, calls, config, values, snapshot = self.control_fixture(directory)
            with self.assertRaises(ControlUnavailable):
                adapter.apply_ir_power(1, 67, "wrong-observation", threading.Event())
            values[1]["Value"] = "6"
            config.write_text(json.dumps(values))
            with self.assertRaises(ControlUnavailable):
                adapter.apply_ir_power(1, 67, snapshot.observation_key, threading.Event())
            self.assertEqual(0, state["saves"])
            self.assertFalse(any(call["operation"] == "apply" for call in calls))

    def test_edit_started_fault_is_not_suppressed_or_retried_as_read_cancellation(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter, state, calls, config, values, snapshot = self.control_fixture(directory)
            state["fail_after_edit"] = True
            with self.assertRaises(AmbiguousWriteError):
                adapter.apply_ir_power(1, 67, snapshot.observation_key, threading.Event())
            with self.assertRaises(ControlUnavailable):
                adapter.apply_ir_power(1, 67, snapshot.observation_key, threading.Event())
            self.assertEqual(1, sum(call["operation"] == "apply" for call in calls))
            self.assertEqual(0, state["saves"])

    def test_textbox_readback_alone_cannot_claim_save_succeeded(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter, state, calls, config, values, snapshot = self.control_fixture(directory)
            state["skip_persist"] = True
            with patch("windows_ui_adapter.sequence_persistence.verify_after", side_effect=RuntimeError("persisted value did not change")):
                with self.assertRaises(AmbiguousWriteError):
                    adapter.apply_ir_power(1, 67, snapshot.observation_key, threading.Event())
            self.assertEqual("62", json.loads(config.read_text())[0]["Value"])
            self.assertEqual(1, state["saves"])

    def test_preview_mode_never_prepares_or_invokes_save(self):
        adapter, calls = self.make_adapter()
        adapter.connect(TARGET, threading.Event())
        adapter.prepare_mode("preview", threading.Event())
        adapter.read_snapshot(threading.Event())
        self.assertFalse(any(call["operation"] in {"prepare_control", "apply"} for call in calls))

    def test_process_page_preserves_connection_setting_without_guessing_off_as_zero(self):
        responses = iter([
            {"success": True, "machine": "M1", "setting": "62"},
            {"success": True, "machine": "M1", "state": "AUTO", "setting": None, "status_powers": {"1": "0"}, "rows": []},
        ])
        adapter = WindowsUiAdapter(runner=lambda request, stop: next(responses))
        adapter.connect(TARGET, threading.Event())
        self.assertEqual(62, adapter.read_snapshot(threading.Event()).heater1_setting_percent)

    def test_fractional_settings_are_preserved_for_preview(self):
        adapter = WindowsUiAdapter(runner=lambda request, stop: {"success": True, "machine": "M1", "setting": "62.2", "rows": []})
        adapter.connect(TARGET, threading.Event())
        self.assertEqual(62.2, adapter.read_snapshot(threading.Event()).heater1_setting_percent)

    def test_readonly_log_hint_is_bounded_and_ignores_finished_jobs(self):
        with tempfile.TemporaryDirectory() as directory:
            target = MachineWindow("M1", 1234, 5678, "BinderJet", str(Path(directory) / "Operator.exe"))
            log = Path(directory) / "Logs/20261027_M1.log"
            log.parent.mkdir()
            log.write_text("unrelated-prefix" * 30000 + "\n[08:30:00 BJT - AUTO]: AutoPrint started!\n[08:30:01 BJT - AUTO]: Layer: 450 -> Current Z = 1.23\n")
            layer, run = _log_layer_hint(target)
            self.assertEqual(450, layer)
            self.assertIsNotNone(run)
            log.write_text(log.read_text() + "[08:30:02 BJT - AUTO]: AutoPrint finished!\n")
            self.assertIsNone(_log_layer_hint(target)[0])


if __name__ == "__main__":
    unittest.main()
