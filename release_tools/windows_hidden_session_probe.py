"""Synthetic Windows hidden-Sequence recovery through the real session stack.

Usage: python release_tools/windows_hidden_session_probe.py PACKAGE_ROOT SOURCE_ROOT OUTPUT_JSON [--window-transitions]

Only owned temporary WinForms fixtures, CSVs and parameter files are used.
Recovery must not edit, Save or navigate. Four subsequent fresh observations
exercise the unchanged normal native apply path with a live cap of 60 percent.
"""
from __future__ import annotations

import csv
import ctypes
from datetime import datetime, timedelta
import hashlib
import io
import json
import math
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
import traceback


COVER_SOURCE = r'''using System;using System.IO;using System.Windows.Forms;
class Cover { [STAThread] static void Main(string[] a) {
var f=new Form {Text="Owned synthetic unrelated foreground",Width=900,Height=700,TopMost=true};
f.Shown+=delegate {f.Activate();File.WriteAllText(a[0],System.Diagnostics.Process.GetCurrentProcess().Id+","+f.Handle.ToInt64());};
Application.Run(f); } }'''
FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def csv_bytes(rows):
    stream = io.StringIO(newline="")
    csv.writer(stream, lineterminator="\r\n").writerows(rows)
    return stream.getvalue().encode("utf-8")


def wait_until(predicate, timeout, message):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            value = predicate()
        except (OSError, ValueError):
            value = None
        if value:
            return value
        time.sleep(.025)
    raise AssertionError(message)


def fields_for(exe, machine, marker):
    from sequence_field_audit import field_bindings
    configs = exe.parent / "Machines" / machine / "Configs"
    configs.mkdir(parents=True, exist_ok=True)
    owners = {"BJT": [], "MachineProfile": []}
    fields = {}
    for control, (scope, name, kind) in field_bindings(machine).items():
        if kind == "toggle":
            value, parameter_type, fields[control] = True, "BoolParameter", "toggle:1"
        elif control == "textBoxHeatingOutput":
            value, parameter_type, fields[control] = "80", "DoubleParameter", "80"
        elif name.startswith("Printhead") and name.endswith("Port"):
            value, parameter_type, fields[control] = "fixture-port", "StringParameter", "fixture-port"
        else:
            value = "5"
            parameter_type = "IntParameter" if "Count" in name or "Interval" in name else "DoubleParameter"
            fields[control] = value
        owner = "BJT" if scope == "Printing" else "MachineProfile"
        owners[owner].append({"Scope": scope, "Name": name, "Type": parameter_type, "Value": value})
    for owner, entries in owners.items():
        (configs / (owner + ".parameters.json")).write_text(json.dumps(entries), encoding="utf-8")
    spec = Path(str(marker) + ".fields.json")
    spec.write_text(json.dumps(fields), encoding="utf-8")
    return spec, len(fields)


class ForegroundCover:
    def __init__(self, executable, ready, target_pid):
        self.process = None
        self.samples = []
        self.stopped = threading.Event()
        self.thread = None
        self.sampling_lock = threading.Lock()
        self.sampling_enabled = True
        self.excluded_transitions = []
        self.user32 = ctypes.windll.user32
        self.user32.GetForegroundWindow.restype = ctypes.c_void_p
        self.hwnd = None
        ready.unlink(missing_ok=True)
        try:
            self.process = subprocess.Popen([str(executable), str(ready)], creationflags=FLAGS)
            wait_until(lambda: ready.exists() or self.process.poll() is not None, 10, "Cover startup timed out")
            if self.process.poll() is not None:
                raise AssertionError("Owned cover exited before readiness")
            pid, self.hwnd = map(int, ready.read_text().split(","))
            assert pid == self.process.pid and pid != target_pid
            wait_until(lambda: self.user32.GetForegroundWindow() == self.hwnd, 5,
                       "Owned unrelated cover did not gain foreground")
            self.thread = threading.Thread(target=self._sample, daemon=True)
            self.thread.start()
        except BaseException:
            self.close()
            raise

    def _sample(self):
        while not self.stopped.wait(.02):
            with self.sampling_lock:
                if self.sampling_enabled:
                    self.samples.append(self.user32.GetForegroundWindow() == self.hwnd)

    def begin_operator_transition(self, label):
        with self.sampling_lock:
            assert self.sampling_enabled
            self.sampling_enabled = False
            self.excluded_transitions.append({"label": label, "begin_monotonic": time.monotonic()})

    def end_operator_transition(self):
        self.user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
        self.user32.SetForegroundWindow.restype = ctypes.c_int
        self.user32.SetForegroundWindow(self.hwnd)
        wait_until(lambda: self.user32.GetForegroundWindow() == self.hwnd, 5,
                   "Owned cover did not resume foreground after operator transition")
        with self.sampling_lock:
            self.excluded_transitions[-1].update(end_monotonic=time.monotonic(), foreground_restored=True)
            self.sampling_enabled = True

    def evidence(self):
        return {"foreground_maintained": bool(self.samples) and all(self.samples)
                    and self.user32.GetForegroundWindow() == self.hwnd,
                "foreground_samples": len(self.samples), "cover_hwnd": self.hwnd,
                "cover_pid": self.process.pid if self.process else None,
                "explicit_operator_transition_exclusions": list(self.excluded_transitions)}

    def close(self):
        self.stopped.set()
        if self.thread is not None:
            self.thread.join(2)
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)


def exercise(exe, cover_exe, folder, machine, minimized, *, window_transitions=False):
    from adapter_contracts import MachineWindow
    from csv_source import MonitoringCsvReader
    from layer_charts import LayerHistory
    from observed_session import run_observed_session
    from resilient_adapter import ResilientUiAdapter
    from sequence_field_audit import assert_fields_match_persisted
    import sequence_persistence
    from session import SessionOptions, run_ui_session
    from windows_ui_adapter import WindowsUiAdapter

    label = machine + ("-minimized" if minimized else "-covered")
    marker = folder / label
    spec, field_count = fields_for(exe, machine, marker)
    started = datetime.now().replace(microsecond=0) - timedelta(minutes=5)
    monitor = folder / "Monitoring" / label
    job = monitor / (started.strftime("%Y%m%d_%H%M%S") + "_1820-layer")
    job.mkdir(parents=True)
    csv_path = job / ("LayerDatasheet_" + started.strftime("%Y%m%d_%H%M%S") + ".csv")

    def row(layer, finish):
        return [str(layer + 1), started.strftime("%m/%d/%Y %H:%M:%S"),
                (finish - timedelta(seconds=24)).strftime("%m/%d/%Y %H:%M:%S"),
                finish.strftime("%m/%d/%Y %H:%M:%S"), "42"]

    csv_path.write_bytes(csv_bytes([list(MonitoringCsvReader.REQUIRED),
                                    row(0, started + timedelta(seconds=60))]))
    log_folder = exe.parent / "Logs"
    log_folder.mkdir(exist_ok=True)
    log = log_folder / ("20261027_" + machine + ".log")
    prefix = started.strftime("%Y-%m-%d%p%I%M%S") + "...[BJT - AUTO]: "
    log.write_text(prefix + "AutoPrint started!\n" + prefix + "Layer: 0 -> Current Z = 1.23\n", encoding="utf-8")
    fixture_args = [str(exe), str(marker), machine, "backgroundcsv", str(spec)]
    if window_transitions:
        fixture_args += ["windowaudit", "visible"]
    fixture = subprocess.Popen(fixture_args, creationflags=FLAGS)
    stop = threading.Event()
    events = queue.Queue()
    worker = None
    cover = None
    wrapper = None
    raw_adapters = []
    native_calls = []
    recoveries = []
    outages = []
    runner_calls = []
    event_counts = {}
    snapshot_log = []
    eligible_emitted_layers = set()
    ineligible_emissions = []
    history = LayerHistory()
    history_lock = threading.Lock()
    history_segment = history.segment_id
    phase = ["initial"]
    next_layer = [0]
    target = before = options = None
    result = {"machine": machine, "minimized": minimized, "page": "webcam", "passed": False,
              "expected_full_profile_fields": field_count}
    window_steps = []
    if window_transitions:
        result["window_mode_steps"] = window_steps

    def save_count():
        return int(Path(str(marker) + ".saves").read_text())

    def navigation_count():
        return int(Path(str(marker) + ".sequence-clicks").read_text())

    def command(**values):
        done = Path(str(marker) + ".command-done")
        done.unlink(missing_ok=True)
        pending = Path(str(marker) + ".command.tmp")
        pending.write_text(json.dumps(values), encoding="utf-8")
        os.replace(pending, Path(str(marker) + ".command"))
        wait_until(lambda: done.exists() and json.loads(done.read_text(encoding="utf-8-sig")) == values,
                   5, "Owned fixture command timed out: " + repr(values))

    def emit(kind, payload):
        event_counts[kind] = event_counts.get(kind, 0) + 1
        if kind == "snapshot":
            snap = payload["snapshot"]
            layer_valid = type(snap.layer_index) is int and snap.layer_index >= 0
            temperature = snap.t_recoating_c
            temperature_valid = (isinstance(temperature, (int, float)) and not isinstance(temperature, bool)
                and math.isfinite(temperature) and -273.15 <= temperature <= 2000)
            key_present = snap.observation_key is not None
            eligible = layer_valid and temperature_valid and key_present
            expired_key = getattr(snap, "expired_observation_key", None)
            measurement_valid = getattr(snap, "measurement_valid", None)
            fresh = getattr(snap, "observation_fresh", None)
            entry = {"phase": phase[0], "emitted_monotonic": time.monotonic(),
                "layer_index": snap.layer_index, "observation_key_present": key_present,
                "observation_fresh": fresh, "expired_observation_key": expired_key,
                "t_recoating_c": temperature, "measurement_valid": measurement_valid,
                "chart_eligible": eligible, "control_available": snap.control_available,
                "epoch": snap.control_epoch, "effective_mode": payload.get("effective_mode"),
                "status": payload.get("status"), "applied": dict(payload.get("applied", {})),
                "reason": snap.control_unavailable_reason}
            with history_lock:
                if eligible:
                    eligible_emitted_layers.add(snap.layer_index + 1)
                else:
                    if not layer_valid:
                        cause = "No valid observed layer"
                    elif not temperature_valid:
                        cause = "No valid observed temperature"
                    elif expired_key:
                        cause = "Observation expired; production freshness guard removed its admitted key"
                    elif measurement_valid is False:
                        cause = "Invalid measurement has no admitted observation key"
                    elif fresh is False:
                        cause = "Observation is not fresh and has no admitted key"
                    else:
                        cause = "No admitted observation key; CSV validation or admission is pending"
                    ineligible_emissions.append(dict(entry, ineligible_cause=cause))
                entry["chart_record_accepted"] = history.record(payload)
            snapshot_log.append(entry)
        events.put((kind, payload))

    def receive(layer, *, applied=None, predicate=None, timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                kind, payload = events.get(timeout=.25)
            except queue.Empty:
                if worker is not None and not worker.is_alive():
                    raise AssertionError("Observed session worker stopped unexpectedly")
                continue
            if kind in {"error", "observer_hold", "actuation_barrier"}:
                raise AssertionError(kind + ": " + str(payload))
            if kind == "snapshot" and payload["snapshot"].layer_index == layer:
                if applied is not None and payload.get("applied", {}).get(1) != applied:
                    continue
                if predicate is not None and not predicate(payload):
                    continue
                return payload
        raise AssertionError("Expected native session observation was not received: " + str(layer))

    def append_fresh():
        next_layer[0] += 1
        layer = next_layer[0]
        command(layer=layer + 1)
        with csv_path.open("ab") as stream:
            stream.write(csv_bytes([row(layer, datetime.now().replace(microsecond=0))]))
        return layer

    def no_recovery_writes(navigation):
        assert save_count() == 0, "Recovery or baseline unexpectedly Saved"
        assert navigation_count() == navigation, "Hidden recovery navigated"
        current = sequence_persistence.capture(target)
        assert current.power == 80 and current.non_ir == before.non_ir

    def window_state():
        return wait_until(lambda: json.loads(Path(str(marker) + ".window-state").read_text(encoding="utf-8-sig")),
                          5, "Fixture native window facts unavailable")

    def check_window_state(mode, state, reference):
        assert state["mode"] == mode
        assert state["handles_present"] is True and reference["handles_present"] is True
        for key in ("root_hwnd", "scope_hwnd", "ir_hwnd", "save_hwnd"):
            assert type(state[key]) is int and state[key] > 0
            assert state[key] == reference[key], ("Window/control recreated", mode, key, reference[key], state[key])
        assert state["root_hwnd"] == target.hwnd
        if mode == "minimized":
            assert state["iconic"] is True and state["window_state"] == "Minimized"
            assert state["native_style"] & 0x20000000
        else:
            assert state["iconic"] is False
            if mode == "maximized":
                assert state["zoomed"] is True and state["window_state"] == "Maximized"
                assert state["native_style"] & 0x01000000
            elif mode == "fullscreen":
                assert state["border_style"] == "None" and state["bounds"] == state["screen_bounds"]
                assert state["native_style"] & 0x00C40000 == 0
                assert state["client_width"] == state["bounds"][2] - state["bounds"][0]
                assert state["client_height"] == state["bounds"][3] - state["bounds"][1]
            else:
                assert state["border_style"] == "Sizable" and state["window_state"] == "Normal"
                assert state["native_style"] & 0x00C40000 == 0x00C40000
                width,height = (640,400) if mode == "normal_small" else (1100,800) if mode == "normal_large" else (760,600)
                assert state["bounds"][2] - state["bounds"][0] == width
                assert state["bounds"][3] - state["bounds"][1] == height

    def healthy(payload):
        cap = options._maximum_ir_setting.status()
        return (payload["snapshot"].control_available and payload["effective_mode"] == "control"
                and not cap["pending"] and cap["active_stamp"] == cap["requested_stamp"]
                and cap["active_effective_cap"] == 80
                and payload.get("status") == "T_recoating 수신 · 기존 설정 유지(저장 없음)")

    try:
        ready = Path(str(marker) + ".window")
        wait_until(lambda: ready.exists() or fixture.poll() is not None, 10, "Owned fixture did not become ready")
        assert fixture.poll() is None
        pid, hwnd = map(int, ready.read_text().split(","))
        assert pid == fixture.pid
        target = MachineWindow(machine, pid, hwnd, "Owned hidden recovery session fixture", str(exe.resolve()))
        before = sequence_persistence.capture(target)
        assert before.power == 80
        options = SessionOptions(target, mode="control", maximum_ir_percent=80)

        def own_discovery():
            assert fixture.poll() is None and fixture.pid == target.pid
            assert Path(target.image_path).resolve() == exe.resolve()
            return [target]

        def raw_factory(*args, **kwargs):
            raw = WindowsUiAdapter(*args, **kwargs)
            raw_adapters.append(raw)
            original_request = raw._request
            original_recover = raw._recover_control

            def request(request_data, *request_args, **request_kwargs):
                begin = time.monotonic()
                entry = {"phase": phase[0], "operation": request_data["operation"],
                         "began_monotonic": begin, "error": None}
                try:
                    response = original_request(request_data, *request_args, **request_kwargs)
                    entry.update(success=True, scope_proof=bool(response.get("handles", {}).get("scope_proof")),
                                 fields=len(response.get("field_map", {})))
                    return response
                except BaseException as error:
                    entry.update(success=False, error={"type": type(error).__name__, "message": str(error)})
                    raise
                finally:
                    entry["seconds"] = round(time.monotonic() - begin, 4)
                    native_calls.append(entry)

            def recover(*recover_args, **recover_kwargs):
                old_epoch = raw._control_epoch
                count = len(native_calls)
                try:
                    return original_recover(*recover_args, **recover_kwargs)
                finally:
                    invoked = [call for call in native_calls[count:] if call["operation"] == "recover_control"]
                    if invoked:
                        recoveries.append({"phase": phase[0], "before_epoch": old_epoch,
                            "after_epoch": raw._control_epoch, "hold_reason": raw._control_hold_reason,
                            "candidate_pending": raw._recovery_candidate is not None,
                            "successful_native_reads": sum(call["success"] for call in invoked),
                            "scope_proof": bool(raw._native_handles.get("scope_proof")),
                            "field_count": len(raw._native_fields)})

            raw._request = request
            raw._recover_control = recover
            return raw

        wrapper = ResilientUiAdapter(raw_factory=raw_factory, discoverer=own_discovery,
            source_factory=lambda chosen: MonitoringCsvReader(chosen, roots=[monitor]))
        original_outage = wrapper._outage

        def outage(reason, *outage_args, **outage_kwargs):
            outages.append({"phase": phase[0], "reason": str(reason), "kwargs": dict(outage_kwargs)})
            return original_outage(reason, *outage_args, **outage_kwargs)

        wrapper._outage = outage

        def runner(*runner_args, **runner_kwargs):
            runner_calls.append({"mode": runner_args[0].mode, "phase": phase[0]})
            return run_ui_session(*runner_args, **runner_kwargs)

        def run():
            try:
                run_observed_session(options, stop, emit, adapter_factory=lambda: wrapper, runner=runner)
            except BaseException as error:
                events.put(("error", type(error).__name__ + ": " + str(error)))

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        receive(0)
        baseline_deadline = time.monotonic() + 45
        baseline_layer = None
        while time.monotonic() < baseline_deadline:
            layer = append_fresh()
            payload = receive(layer)
            assert save_count() == 0
            if healthy(payload):
                baseline_layer = layer
                break
        assert baseline_layer is not None, "Full production stack did not reach a healthy visible baseline"
        raw = wrapper._raw
        assert raw is not None and raw._native_handles.get("scope_proof")
        assert len(raw._native_fields) == field_count
        assert_fields_match_persisted(raw._control_baseline, sequence_persistence.capture(target), machine)
        raw_id, reader_id, raw_epoch = id(raw), id(wrapper._reader), raw._control_epoch
        baseline_navigation = navigation_count()
        assert baseline_navigation == 1, "Initial fixture did not prepare via the normal single Sequence navigation"
        with history_lock:
            baseline_history = tuple(point.layer for point in history.points)
        result.update(initial_baseline_layer=baseline_layer + 1, initial_navigation_count=baseline_navigation,
                      initial_native_epoch=raw_epoch, initial_full_profile_audited=True)

        command(page="webcam", minimized=minimized)
        cover = ForegroundCover(cover_exe, Path(str(marker) + ".cover-ready"), target.pid)
        phase[0] = "hidden-recovery"
        wrapper.suspend_control("synthetic hidden recovery")
        recovery_deadline = time.monotonic() + 45
        recovered_layer = None
        while time.monotonic() < recovery_deadline:
            layer = append_fresh()
            payload = receive(layer)
            no_recovery_writes(baseline_navigation)
            assert id(wrapper._raw) == raw_id and id(wrapper._reader) == reader_id
            if healthy(payload) and raw._control_epoch > raw_epoch:
                recovered_layer = layer
                break
        assert recovered_layer is not None, "Hidden recovery did not return through native baseline/fresh-layer barriers"
        actual_hidden_reads = [call for call in native_calls if call["phase"] == "hidden-recovery"
                               and call["operation"] == "recover_control" and call["success"]]
        assert len(actual_hidden_reads) >= 2, "Fewer than two actual native recovery confirmations"
        assert any(item["candidate_pending"] and item["before_epoch"] == item["after_epoch"]
                   for item in recoveries if item["phase"] == "hidden-recovery")
        assert sum(item["after_epoch"] > item["before_epoch"] for item in recoveries
                   if item["phase"] == "hidden-recovery") == 1
        assert any(not item["control_available"] for item in snapshot_log if item["phase"] == "hidden-recovery")
        assert not outages and len(runner_calls) == 1 and len(raw_adapters) == 1
        assert event_counts.get("connected") == 1 and not event_counts.get("observer_hold", 0)
        with history_lock:
            recovered_history = tuple(point.layer for point in history.points)
            assert history.segment_id == history_segment
        assert recovered_history[:len(baseline_history)] == baseline_history
        assert_fields_match_persisted(raw._control_baseline, sequence_persistence.capture(target), machine)
        no_recovery_writes(baseline_navigation)
        result.update(recovery_completed_layer=recovered_layer + 1, recovery_native_reads=len(actual_hidden_reads),
                      recovery_saves=0, recovery_navigation=0, same_reader=True, same_native_adapter=True,
                      same_observed_session=True, chart_history_preserved=True)

        phase[0] = "live-cap-apply"
        assert options._maximum_ir_setting.set(60)
        receive(recovered_layer)
        no_recovery_writes(baseline_navigation)
        powers, counts, applied_layers = [], [], []
        for order, expected in enumerate((75, 70, 65, 60), 1):
            layer = append_fresh()
            payload = receive(layer, applied=expected)
            assert payload["snapshot"].t_recoating_c == 42
            assert payload["snapshot"].observation_key is not None
            assert payload["snapshot"].observation_fresh is True
            assert payload["snapshot"].measurement_valid is True
            current = sequence_persistence.capture(target)
            powers.append(current.power)
            counts.append(save_count())
            applied_layers.append(layer + 1)
            assert current.power == expected and current.non_ir == before.non_ir
            assert counts[-1] == order and navigation_count() == baseline_navigation
            receive(layer)
            receive(layer)
            assert save_count() == order, "A duplicate or cached row Saved again"
            assert id(wrapper._raw) == raw_id and id(wrapper._reader) == reader_id
        if window_transitions:
            reference = window_state()
            previous = reference
            sequence = (("normal_small", "sequence"), ("maximized", "webcam"),
                        ("normal_large", "sequence"), ("fullscreen", "webcam"),
                        ("windowed_restore", "sequence"), ("minimized", "webcam"),
                        ("normal_restore", "webcam"))
            for index,(mode,page) in enumerate(sequence, 1):
                phase[0] = "window-" + mode
                step = {"mode": mode, "page": page, "stage": "before_command", "native_before": previous,
                        "passed": False, "fresh_layers": []}
                window_steps.append(step)
                before_count = save_count()
                current = sequence_persistence.capture(target)
                before_power = current.power
                assert before_power == 65 - index * 5
                cover.begin_operator_transition(mode)
                command(window_mode=mode, page=page)
                facts = window_state()
                step.update(native_after_command=facts, stage="native_after_command")
                check_window_state(mode, facts, reference)
                assert facts["ir_bounds"] != previous["ir_bounds"], ("IR rectangle did not move", mode)
                assert facts["save_bounds"] != previous["save_bounds"], ("Save rectangle did not move", mode)
                current = sequence_persistence.capture(target)
                assert current.power == before_power and current.non_ir == before.non_ir
                assert save_count() == before_count and navigation_count() == baseline_navigation
                cover.end_operator_transition()
                step["stage"] = "foreground_restored_before_fresh_row"
                samples_before = len(cover.samples)
                wait_until(lambda: len(cover.samples) > samples_before and cover.samples[-1] is True,
                           5, "Cover sampling did not resume before controllable input")
                assert cover.evidence()["foreground_maintained"]
                step["foreground_samples_before_fresh_row"] = len(cover.samples) - samples_before
                expected = before_power - 5
                assert options._maximum_ir_setting.set(expected)
                # Same already-processed row: mode/cap changes must not Save it.
                receive(next_layer[0]); receive(next_layer[0])
                assert save_count() == before_count
                step_layers = step["fresh_layers"]
                completed = False
                for _ in range(4):
                    layer = append_fresh()
                    step_layers.append(layer + 1)
                    payload = receive(layer)
                    if payload.get("applied", {}).get(1) == expected:
                        assert payload["snapshot"].observation_fresh is True
                        assert payload["snapshot"].measurement_valid is True
                        assert payload["snapshot"].observation_key is not None
                        completed = True
                        break
                    assert not payload.get("applied"), payload.get("applied")
                    assert save_count() == before_count
                assert completed, ("Feedback did not resume after window mode change", mode)
                receive(layer); receive(layer)
                current = sequence_persistence.capture(target)
                assert current.power == expected and current.non_ir == before.non_ir
                assert save_count() == before_count + 1 and navigation_count() == baseline_navigation
                assert id(wrapper._raw) == raw_id and id(wrapper._reader) == reader_id
                after = window_state(); check_window_state(mode, after, reference)
                step["native_after_save"] = after
                assert len(cover.samples) > samples_before and cover.evidence()["foreground_maintained"]
                powers.append(current.power); counts.append(save_count()); applied_layers.append(layer + 1)
                step.update(power=current.power, save_count=save_count(), no_save_from_transition_or_old_row=True,
                    foreground_samples_during_control=len(cover.samples) - samples_before, stage="complete", passed=True)
                previous = facts
        assert not Path(str(marker) + ".table-access").exists(), "Forbidden Process access was recorded"
        assert not any(call["operation"] in {"read", "return_process", "open_process", "follow_row"}
                       for call in native_calls)
        assert not any(call["operation"] == "enter_sequence" and call["phase"] != "initial" for call in native_calls)
        state = json.loads(Path(str(marker) + ".background-state").read_text(encoding="utf-8-sig"))
        assert state["page"] == "webcam" and state["minimized"] == (False if window_transitions else minimized), state
        foreground = cover.evidence()
        assert foreground["foreground_maintained"]
        with history_lock:
            final_layers = tuple(point.layer for point in history.points)
            expected_observed_layers = tuple(sorted(eligible_emitted_layers))
            assert history.segment_id == history_segment
        # A row can expire during the two bounded native recovery reads. Such a
        # snapshot intentionally has no admitted key and is not a chart input.
        # Require every eligible emitted observation to survive, without adding
        # invented points for emitted-but-ineligible or unobserved CSV rows.
        assert final_layers == expected_observed_layers, (final_layers, expected_observed_layers)
        assert final_layers[:len(baseline_history)] == baseline_history
        assert set(applied_layers).issubset(final_layers), (applied_layers, final_layers)
        assert set(final_layers).issubset(range(1, next_layer[0] + 2)), final_layers
        assert final_layers[-1] == next_layer[0] + 1
        assert not outages and len(runner_calls) == 1 and len(raw_adapters) == 1
        assert event_counts.get("connected") == 1 and not event_counts.get("actuation_barrier", 0)
        if window_transitions:
            assert len(window_steps) == 7 and len(powers) == 11 and counts == list(range(1,12))
            assert all(step["passed"] for step in window_steps)
            result.update(all_seven_transitions_passed=True, root_and_control_handles_preserved=True,
                          all_eleven_applied_fresh_layers_retained=True)
        result.update(passed=True, powers=powers, save_counts=counts, applied_layers=applied_layers,
            no_duplicate_or_old_row_saves=True, final_history_layers=list(final_layers),
            eligible_emitted_layers=list(expected_observed_layers),
            emitted_but_ineligible=list(ineligible_emissions), all_eligible_chart_observations_preserved=True,
            all_four_applied_fresh_layers_retained=True,
            non_ir_unchanged=True, process_table_accesses=0, background_state=state,
            cap_status=options._maximum_ir_setting.status(), **foreground)
    except BaseException as error:
        result.update(passed=False,error=type(error).__name__ + ": " + str(error), traceback=traceback.format_exc())
    finally:
        # Snapshot evidence before stopping/killing the owned target. Do not
        # replace a native failure with secondary diagnostics or teardown errors.
        capture = {"fixture_running": fixture.poll() is None, "phase": phase[0],
                   "native_calls": list(native_calls), "native_recoveries": list(recoveries),
                   "wrapper_outages": list(outages), "runner_calls": list(runner_calls),
                   "event_counts": dict(event_counts), "snapshots": list(snapshot_log)}
        try:
            capture.update(saves=save_count(), navigation=navigation_count(),
                background_state=json.loads(Path(str(marker) + ".background-state").read_text(encoding="utf-8-sig")))
            if target is not None:
                current = sequence_persistence.capture(target)
                capture.update(power=current.power, non_ir_unchanged=before is not None and current.non_ir == before.non_ir,
                    non_ir_sha256=hashlib.sha256(repr(sorted(current.non_ir.items())).encode()).hexdigest())
            if cover is not None:
                capture.update(cover.evidence())
            if window_transitions:
                capture["native_window_state"] = window_state()
                events_file = Path(str(marker) + ".handle-events")
                capture["fixture_handle_events"] = events_file.read_text().splitlines() if events_file.exists() else []
            error_file = Path(str(marker) + ".error")
            if error_file.exists():
                capture["fixture_ui_error"] = error_file.read_text()
                result.update(passed=False, fixture_ui_error=capture["fixture_ui_error"])
            with history_lock:
                capture["history_layers"] = [point.layer for point in history.points]
                capture["history_segment"] = history.segment_id
                capture["eligible_emitted_layers"] = sorted(eligible_emitted_layers)
                capture["emitted_but_ineligible"] = list(ineligible_emissions)
        except BaseException as error:
            capture["capture_error"] = type(error).__name__ + ": " + str(error)
            result.update(passed=False, evidence_capture_error=capture["capture_error"])
        result["readonly_pre_teardown"] = capture
        stop.set()
        if worker is not None:
            worker.join(8)
            result["worker_retired"] = not worker.is_alive()
            if worker.is_alive():
                result.update(passed=False, teardown_error="Observed session worker did not retire")
        if cover is not None:
            cover.close()
        if fixture.poll() is None:
            fixture.terminate()
        try:
            fixture.wait(timeout=5)
        except subprocess.TimeoutExpired:
            fixture.kill()
            fixture.wait(timeout=5)
        log.unlink(missing_ok=True)
    return result


def main(package, source_root, output, *, window_transitions=False):
    report = {"passed": False, "cases": [],
        "scope": "Owned synthetic Windows full-profile fixtures and temporary CSV/settings only; real observed/resilient/session/native helper stack.",
        "untested": ["Real BJT UI, devices, sensor/PLC/lamp output, Excel COM and locked desktop.",
                     "Discovery uses an identity-checked fixture-only callback; global discovery is not exercised.",
                     "Borderless fullscreen is synthetic WinForms; real BJT fullscreen and DPI/monitor changes are not validated."]}
    report["window_transition_audit"] = window_transitions
    try:
        if os.name != "nt":
            raise RuntimeError("Windows required")
        sys.path.insert(0, str(package / "app"))
        fixture_source = source_root / "validation" / "CsvRouteFixture.cs"
        report["source_sha256"] = {"probe": sha256(__file__), "fixture": sha256(fixture_source),
            **{name: sha256(package / "app" / name) for name in
                ("native_helper.ps1", "UiaBridge.dll", "session.py", "observed_session.py", "resilient_adapter.py")}}
        with tempfile.TemporaryDirectory(prefix="hidden-session-native-") as temporary:
            folder = Path(temporary)
            binary = folder / "fullroutebin"
            binary.mkdir()
            executable, cover_exe = binary / "CsvRouteFixture.exe", folder / "Cover.exe"
            cover_source = folder / "Cover.cs"
            cover_source.write_text(COVER_SOURCE, encoding="utf-8")
            compiler = Path(os.environ["WINDIR"]) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
            report["fixture_compilation"] = []
            for destination, source, references in (
                (executable, fixture_source, ("System.Windows.Forms.dll", "System.Drawing.dll", "System.Web.Extensions.dll")),
                (cover_exe, cover_source, ("System.Windows.Forms.dll", "System.Drawing.dll"))):
                command = [str(compiler), "/nologo", "/target:winexe", "/platform:x64", "/out:" + str(destination)]
                command += ["/reference:" + item for item in references] + [str(source)]
                completed = subprocess.run(command, capture_output=True, text=True, timeout=30)
                report["fixture_compilation"].append({"source": source.name, "returncode": completed.returncode,
                    "stdout": completed.stdout, "stderr": completed.stderr})
                if completed.returncode:
                    raise RuntimeError("Owned fixture compilation failed")
            for machine, minimized in (("M1", False), ("M1", True), ("S3", False), ("S3", True)):
                report["cases"].append(exercise(executable, cover_exe, folder, machine, minimized,
                                                window_transitions=window_transitions))
            report["passed"] = len(report["cases"]) == 4 and all(case["passed"] for case in report["cases"])
    except BaseException as error:
        report.update(error=type(error).__name__ + ": " + str(error), traceback=traceback.format_exc())
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    if len(sys.argv) not in (4,5) or (len(sys.argv)==5 and sys.argv[4]!="--window-transitions"):
        raise SystemExit(__doc__)
    raise SystemExit(main(*(Path(value).resolve() for value in sys.argv[1:4]), window_transitions=len(sys.argv)==5))
