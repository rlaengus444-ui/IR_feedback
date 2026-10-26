"""Standalone Windows UI Automation reader for an unchanged BinderJet process.

Every provider operation runs in a disposable, time-bounded helper. Explicit
control mode uses the user's existing Sequence IR textbox and Save procedure.
The adapter never injects code, toggles a heater, or opens a communication port.
"""
from __future__ import annotations

import base64
import hashlib
import json
import itertools
import math
import os
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable

try:
    from .adapter_contracts import AdapterError, AmbiguousWriteError, ControlUnavailable, MachineWindow, UiSnapshot
    from . import sequence_persistence
    from .sequence_field_audit import assert_fields_match_persisted
except ImportError:
    from adapter_contracts import AdapterError, AmbiguousWriteError, ControlUnavailable, MachineWindow, UiSnapshot
    import sequence_persistence
    from sequence_field_audit import assert_fields_match_persisted


CONTROL_REASON = "기존 Sequence IR 입력·Save 경로와 저장 파일을 아직 확인하지 못했습니다."

# Tests can inject a synthetic-only helper. Production uses the adjacent
# native dispatcher; its sole UIA operation is isolated leaf-ID metadata.
_HELPER = ""



def _number(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    text = str(value).strip().replace("°C", "").replace("℃", "").strip()
    if not re.fullmatch(r"[-+]?\d+(?:[.,]\d+)?", text):
        return None
    result = float(text.replace(",", "."))
    return result if math.isfinite(result) else None


def _log_layer_hint(target: MachineWindow) -> tuple[int | None, str | None]:
    """Read an existing log tail only; it supplies coordinates, never temperature."""
    if target.machine not in {"M1", "S3"} or not target.image_path:
        return None, None
    directory = Path(target.image_path).parent / "Logs"
    try:
        with os.scandir(directory) as entries:
            candidates = [Path(entry.path) for entry in itertools.islice(entries, 4096)
                          if entry.is_file() and entry.name.lower().endswith("_" + target.machine.lower() + ".log")]
        if not candidates:
            return None, None
        path = max(candidates, key=lambda item: item.stat().st_mtime_ns)
        stat = path.stat()
        age = time.time() - stat.st_mtime
        if age < -5 or age > 15:
            return None, None
        with path.open("rb") as stream:
            stream.seek(max(0, stat.st_size - 262144))
            tail = stream.read(262144).decode("utf-8", errors="replace")
        layer = None
        run = None
        for line in tail.splitlines():
            if "AUTO" not in line:
                continue
            if "AutoPrint started!" in line:
                run = hashlib.sha256((str(path) + "\0" + line).encode()).hexdigest()
                layer = None
            match = re.search(r"\bLayer:\s*(\d+)\s*->\s*Current Z\s*=", line)
            if match:
                layer = int(match.group(1))
            if any(terminal in line for terminal in ("AutoPrint finished!", "AutoPrint cancelled.", "Stopping printing as requested.")):
                layer = None
        return layer, run
    except (OSError, ValueError):
        return None, None


class WindowsUiAdapter:
    MINIMUM_POLL_SECONDS = 1.0

    def __init__(self, helper_timeout: float = 3.0, *, runner: Callable | None = None):
        if not 0.1 <= helper_timeout <= 10:
            raise ValueError("UI helper timeout must be in 0.1..10 seconds.")
        self.helper_timeout = float(helper_timeout)
        self._runner = runner
        self._lock = threading.Lock()
        self._helper_lock = threading.Lock()
        self._active_helper: subprocess.Popen | None = None
        self._target: MachineWindow | None = None
        self._generation = 0
        self._last_read = 0.0
        self._last_snapshot: UiSnapshot | None = None
        self._last_settings: list[int | float | None] = [None, None]
        self._mode = "monitor"
        self._control_baseline: dict | None = None
        self._persisted_baseline = None
        self._expected_ir: str | None = None
        self._observation_row: dict | None = None
        self._control_fault: str | None = None
        self._run_key: str | None = None
        self._layer_hint: int | None = None
        self._native_handles: dict = {}
        self._native_fields: dict = {}

    def discover(self) -> list[MachineWindow]:
        response = self._request({"operation": "discover"}, None)
        result: list[MachineWindow] = []
        for row in response.get("windows", []):
            try:
                machine = str(row.get("machine", "unknown")).upper()
                result.append(MachineWindow(machine if machine in {"M1", "S3"} else "unknown", int(row["pid"]), int(row["hwnd"]), str(row.get("title", "")), str(row.get("image_path", ""))))
            except (KeyError, TypeError, ValueError):
                continue
        return result

    def connect(self, target: MachineWindow, stop_event) -> None:
        self._check_stopped(stop_event)
        if target.pid <= 0 or target.hwnd <= 0:
            raise AdapterError("유효한 원본 프로그램 창을 선택해야 합니다.")
        response = self._request({"operation": "validate", "pid": target.pid, "hwnd": target.hwnd}, stop_event)
        reported = str(response.get("machine", "unknown")).upper()
        if target.machine in {"M1", "S3"} and reported in {"M1", "S3"} and reported != target.machine:
            raise AdapterError("선택한 창의 장비 종류가 변경되었습니다. 다시 검색하세요.")
        with self._lock:
            self._generation += 1
            self._target = target
            self._last_read = 0
            self._last_snapshot = None
            self._last_settings = [None, None]
            self._control_baseline = None
            self._persisted_baseline = None
            self._control_fault = None
            self._run_key = None
            self._native_handles = dict(response.get("handles") or {})
            self._native_fields = {}
            self._update_settings_locked(response)

    def prepare_mode(self, mode: str, stop_event) -> None:
        self._check_stopped(stop_event)
        if mode not in {"monitor", "preview", "control"}:
            raise ValueError("Unknown UI mode.")
        with self._lock:
            target, generation = self._target, self._generation
            self._mode = mode
            handles = dict(self._native_handles)
        if target is None:
            raise AdapterError("먼저 원본 프로그램 창에 연결하세요.")
        if mode != "control":
            return
        if target.machine not in {"M1", "S3"}:
            raise ControlUnavailable("M1/S3 장비 종류가 확인된 창에서만 자동 적용할 수 있습니다.")
        before = sequence_persistence.capture(target)
        if before is None:
            raise ControlUnavailable("기존 IR 저장 파일을 찾을 수 없어 Save 결과를 확인할 수 없습니다.")
        common = {"pid": target.pid, "hwnd": target.hwnd, "machine": target.machine, "_generation": generation}
        entered = self._request(dict(common, operation="enter_sequence", handles=handles), stop_event)
        metadata = self._request(dict(common, operation="metadata"), stop_event)
        if not entered.get("entered", False):
            present = {str(item.get("id")) for item in metadata.get("records", [])
                       if isinstance(item, dict) and item.get("visible") and item.get("id") in {"textBoxHeatingOutput", "btnSave"}}
            if present and present != {"textBoxHeatingOutput", "btnSave"}:
                raise ControlUnavailable("Sequence 입력 화면이 불완전하여 재입장하거나 저장하지 않고 중단했습니다.")
            if not present:
                self._request(dict(common, operation="enter_sequence", handles=handles, force_sequence=True), stop_event)
                metadata = self._request(dict(common, operation="metadata"), stop_event)
        # The metadata client must exit before native field operations or
        # showing the large virtual Process table again.
        response = self._request(dict(common, operation="prepare_control", metadata=metadata.get("records", []), handles=handles), stop_event)
        fields = response.get("fields")
        displayed = _number(response.get("setting"))
        if not isinstance(fields, dict) or "textBoxHeatingOutput" not in fields or displayed is None or not math.isclose(displayed, before.power, rel_tol=0, abs_tol=1e-9):
            raise ControlUnavailable("Sequence IR 표시값과 기존 저장값이 일치하지 않아 자동 적용을 보류했습니다.")
        try:
            assert_fields_match_persisted(fields, before, target.machine)
        except ValueError as exc:
            raise ControlUnavailable("연결 전 미저장/다른 Sequence 설정을 확인해야 합니다. " + str(exc)) from exc
        after = sequence_persistence.capture(target)
        if after is None or after.sha256 != before.sha256 or after.non_ir != before.non_ir:
            raise ControlUnavailable("연결 준비 중 기존 설정 파일이 변경되었습니다. 다시 연결하세요.")
        handles = dict(response.get("handles") or {})
        native_fields = dict(response.get("field_map") or {})
        self._request(dict(common, operation="return_process", handles=handles), stop_event)
        with self._lock:
            if self._generation != generation or self._target != target:
                raise AdapterError("연결 준비 중 대상이 변경되거나 연결이 해제되었습니다.")
            self._control_baseline = fields
            self._persisted_baseline = after
            self._expected_ir = str(fields["textBoxHeatingOutput"])
            self._native_handles = handles
            self._native_fields = native_fields
            self._last_settings = [before.power, before.power]
            self._last_snapshot = None

    def _update_settings_locked(self, response: dict) -> None:
        displayed = _number(response.get("setting"))
        if displayed is not None and 0 <= displayed <= 100:
            self._last_settings = [displayed, displayed]
        powers = response.get("status_powers", {})
        if isinstance(powers, dict):
            for heater in (1, 2):
                power = _number(powers.get(str(heater)))
                # An OFF status is not a recipe change. Preserve the last known
                # command setting across the normal production off window.
                if power is not None and 0 < power <= 100:
                    self._last_settings[heater - 1] = power

    def read_snapshot(self, stop_event) -> UiSnapshot:
        self._check_stopped(stop_event)
        with self._lock:
            target, generation = self._target, self._generation
            handles = dict(self._native_handles)
            if target is None:
                raise AdapterError("원본 프로그램에 연결되어 있지 않습니다.")
            if self._last_snapshot is not None and time.monotonic() - self._last_read < self.MINIMUM_POLL_SECONDS:
                return self._last_snapshot
        layer_hint, log_run_key = _log_layer_hint(target)
        response = self._request({"operation": "read", "pid": target.pid, "hwnd": target.hwnd,
                                  "machine": target.machine, "handles": handles,
                                  "_generation": generation,
                                  "require_process": self._mode == "control", "follow_row": self._mode == "control",
                                  "layer_hint": layer_hint}, stop_event)
        if layer_hint is None:
            progress = str(response.get("progress", ""))
            match = re.search(r"(?i)\bLayer\s*[:#]?\s*(\d+)\s*/", progress)
            if match:
                layer_hint = max(0, int(match.group(1)) - 1)
        candidates: list[tuple[int, float, str]] = []
        for row in response.get("rows", []):
            if not isinstance(row, dict):
                continue
            temperature = _number(row.get("temperature"))
            start = str(row.get("start", "")).strip()
            try:
                index = int(row["row_index"])
            except (KeyError, TypeError, ValueError):
                continue
            # Original future/unmeasured rows use 0 and DateTime.MinValue. The UI
            # has no measurement-valid bit, so never treat that default as data.
            if temperature is None or temperature == 0 or not -273.15 <= temperature <= 2000 or not start or "0001" in start:
                continue
            candidates.append((index, temperature, start))
        latest = max(candidates, key=lambda row: row[0]) if candidates else None
        persisted_power = None
        try:
            committed = sequence_persistence.capture(target)
            if committed is not None:
                persisted_power = committed.power
        except (RuntimeError, OSError):
            pass
        with self._lock:
            if persisted_power is not None:
                self._last_settings = [persisted_power, persisted_power]
                response = dict(response, setting=None, status_powers={})
            self._update_settings_locked(response)
            settings = tuple(self._last_settings)
            control_ready = self._mode == "control" and self._control_baseline is not None and self._control_fault is None
            observation_current = layer_hint is not None and latest is not None and layer_hint - 1 <= latest[0] <= layer_hint
            initial = next((row for row in candidates if row[0] == 0), None)
            if initial is not None:
                self._run_key = hashlib.sha256(json.dumps([target.pid, target.hwnd, initial[2]]).encode()).hexdigest()
            if log_run_key is not None:
                self._run_key = log_run_key
            run_key = self._run_key
        observation_key = None
        if latest:
            identity = json.dumps([target.pid, target.hwnd, latest[0], latest[2], latest[1]], ensure_ascii=False)
            observation_key = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        if self._mode == "control" and not observation_current:
            observation_key = None
        details = ("원본 표 UI가 제공하는 기록을 읽습니다. Layer 표 자체의 갱신 간격은 1초입니다. "
                   "표시된 IR 설정은 실시간 실출력 확인값이 아닙니다.")
        if not latest:
            details += " 원본의 Process → Layer 표 화면에서 완료된 T_Recoating 행이 보이도록 해주세요."
        if self._mode == "control" and not observation_current:
            details += " 현재 생산 층과 새 T_recoating 확인을 기다리는 중이며 설정을 적용하지 않습니다."
        reason = self._control_fault or CONTROL_REASON
        if target.machine == "unknown":
            reason = "장비 종류를 M1/S3로 확인할 수 없습니다. " + reason
        snapshot = UiSnapshot(target=target, layer_index=latest[0] if latest else None,
                              t_recoating_c=latest[1] if latest else None, observation_key=observation_key,
                              heater1_setting_percent=settings[0], heater2_setting_percent=settings[1],
                              machine_state=str(response.get("state", "unknown")), control_available=control_ready,
                              control_unavailable_reason=reason, details=details, coupled_output=True,
                              minimum_poll_seconds=self.MINIMUM_POLL_SECONDS, run_key=run_key)
        with self._lock:
            if self._generation != generation or self._target != target:
                raise AdapterError("관측 중 연결이 해제되거나 변경되었습니다.")
            self._last_snapshot, self._last_read = snapshot, time.monotonic()
            self._native_handles = dict(response.get("handles") or self._native_handles)
            self._observation_row = {"row_index": latest[0], "temperature": latest[1], "start": latest[2]} if latest else None
            self._layer_hint = layer_hint
        return snapshot

    def apply_ir_power(self, heater: int, power_percent: int | float, expected_observation_key: str, stop_event) -> None:
        self._check_stopped(stop_event)
        if heater not in (1, 2) or isinstance(power_percent, bool) or not isinstance(power_percent, (int, float)) or not math.isfinite(power_percent) or not 0 <= power_percent <= 100:
            raise ValueError("Heater must be 1 or 2 and power must be finite in 0..100.")
        power_text = format(power_percent, ".17g")
        with self._lock:
            target, snapshot = self._target, self._last_snapshot
            baseline, persisted = self._control_baseline, self._persisted_baseline
            expected_ir, observation = self._expected_ir, self._observation_row
            generation = self._generation
            handles = dict(self._native_handles)
            if self._mode != "control" or self._control_fault or baseline is None or persisted is None or target is None:
                raise ControlUnavailable(self._control_fault or CONTROL_REASON)
            if snapshot is None or snapshot.observation_key != expected_observation_key or observation is None or time.monotonic() - self._last_read > 3:
                raise ControlUnavailable("최신 T_recoating 관측값을 다시 확인해야 합니다.")
            if not snapshot.control_available or self._layer_hint is None or not self._layer_hint - 1 <= observation["row_index"] <= self._layer_hint:
                raise ControlUnavailable("현재 생산 층과 일치하는 최신 관측값이 필요합니다.")
            layer_hint = self._layer_hint
        current_hint, _ = _log_layer_hint(target)
        if current_hint is not None:
            if not current_hint - 1 <= observation["row_index"] <= current_hint:
                raise ControlUnavailable("계산 이후 생산 층이 진행되어 이전 관측값의 적용을 중단했습니다.")
            layer_hint = current_hint
        current = sequence_persistence.capture(target)
        expected_number = _number(expected_ir)
        if current is None or expected_number is None or not math.isclose(current.power, expected_number, rel_tol=0, abs_tol=1e-9) or current.non_ir != persisted.non_ir:
            raise ControlUnavailable("기존 IR 또는 다른 저장 설정이 변경되어 자동 적용을 중단했습니다.")
        common = {"pid": target.pid, "hwnd": target.hwnd, "machine": target.machine, "_generation": generation}
        self._request(dict(common, operation="enter_sequence", handles=handles, observation=observation, layer_hint=layer_hint), stop_event)
        guarded_at = time.monotonic()
        metadata = self._request(dict(common, operation="metadata"), stop_event)
        fresh = self._request(dict(common, operation="prepare_control", metadata=metadata.get("records", []), handles=handles), stop_event)
        fresh_fields = fresh.get("fields")
        if not isinstance(fresh_fields, dict) or fresh_fields != dict(baseline, textBoxHeatingOutput=expected_ir):
            raise ControlUnavailable("IR 또는 다른 Sequence 입력값이 변경되어 자동 저장을 중단했습니다.")
        current = sequence_persistence.capture(target)
        if current is None or current.non_ir != persisted.non_ir or not math.isclose(current.power, expected_number, rel_tol=0, abs_tol=1e-9):
            raise ControlUnavailable("IR 입력 전 기존 저장 설정이 변경되었습니다.")
        try:
            assert_fields_match_persisted(fresh_fields, current, target.machine)
        except ValueError as exc:
            raise ControlUnavailable("IR 입력 전에 다른 설정의 미저장 변경을 발견했습니다. " + str(exc)) from exc
        if time.monotonic() - guarded_at > 3:
            raise ControlUnavailable("관측 확인 후 시간이 지나 이번 적용을 중단했습니다. 다시 연결해 새 관측을 확인하세요.")
        handles = dict(fresh.get("handles") or handles)
        native_fields = dict(fresh.get("field_map") or self._native_fields)
        with tempfile.TemporaryDirectory(prefix="binderjet-save-") as directory:
            marker = Path(directory) / "save-started.txt"
            edit_marker = Path(directory) / "edit-started.txt"
            try:
                response = self._request({"operation": "apply", "pid": target.pid, "hwnd": target.hwnd, "machine": target.machine,
                                          "_generation": generation,
                                          "baseline": baseline, "expected_ir": expected_ir, "observation": observation,
                                          "handles": handles, "field_map": native_fields,
                                          "layer_hint": layer_hint,
                                          "follow_row": True,
                                          "power": power_text, "save_marker": str(marker), "edit_marker": str(edit_marker)}, stop_event)
                readback = _number(response.get("setting"))
                if not response.get("save_invoked") or readback is None or not math.isclose(readback, power_percent, rel_tol=0, abs_tol=1e-9):
                    raise AdapterError("IR Save 확인 응답이 일치하지 않습니다.")
                verified = sequence_persistence.verify_after(current, power_percent, timeout=1.0)
                if verified is None:
                    raise AdapterError("IR 저장 결과를 확인할 수 없습니다.")
                self._request(dict(common, operation="return_process", handles=handles), stop_event)
            except Exception as exc:
                with self._lock:
                    self._control_fault = "자동 적용을 중단했습니다. 결과를 확인한 뒤 다시 연결하세요."
                if marker.exists() or edit_marker.exists():
                    raise AmbiguousWriteError("IR 입력값이 수정되었거나 Save가 실행되었을 수 있으나 완료 여부를 확인하지 못했습니다. 같은 명령을 자동 재시도하지 않습니다. " + str(exc)) from exc
                raise
        with self._lock:
            if self._generation != generation or self._target != target:
                raise AmbiguousWriteError("IR 저장 확인 중 연결이 변경되었습니다. 같은 명령을 자동 재시도하지 않습니다.")
            self._persisted_baseline = verified
            self._native_handles = handles
            self._native_fields = native_fields
            self._control_baseline = dict(fresh_fields, textBoxHeatingOutput=str(response["setting"]))
            self._expected_ir = str(response["setting"])
            self._last_settings = [power_percent, power_percent]
            self._last_snapshot = None
            self._last_read = 0

    def disconnect(self) -> None:
        with self._lock:
            self._generation += 1
            self._target = None
            self._last_snapshot = None
            self._control_baseline = None
            self._persisted_baseline = None
            self._native_handles = {}
            self._native_fields = {}
            helper = self._active_helper
        if helper is not None and helper.poll() is None:
            try:
                helper.kill()  # Only the helper process created by this adapter.
            except OSError:
                pass

    @staticmethod
    def _check_stopped(stop_event) -> None:
        if stop_event is not None and stop_event.is_set():
            raise AdapterError("관측이 중지되었습니다.")

    def _request(self, request: dict, stop_event) -> dict:
        self._check_stopped(stop_event)
        request = dict(request, bridge_path=str(Path(__file__).with_name("UiaBridge.dll")))
        expected_generation = request.get("_generation")
        with self._lock:
            if expected_generation is not None and expected_generation != self._generation:
                raise AdapterError("연결이 변경되어 이전 작업을 중단했습니다.")
        if self._runner is not None:
            response = self._runner(request, stop_event)
            if not isinstance(response, dict):
                raise AdapterError("UI 읽기 도우미가 올바른 응답을 반환하지 않았습니다.")
            if not response.get("success", False):
                raise AdapterError(str(response.get("error", "UI 읽기에 실패했습니다.")))
            return response
        if os.name != "nt":
            raise AdapterError("기존 프로그램 화면 연결은 Windows에서만 지원합니다.")
        deadline_seconds = max(self.helper_timeout, 5.0) if request.get("operation") in {"prepare_control", "apply"} else self.helper_timeout
        if not self._helper_lock.acquire(timeout=deadline_seconds):
            raise AdapterError("이전 UI 읽기가 아직 종료되지 않았습니다.")
        process = None
        try:
            helper_directory = tempfile.TemporaryDirectory(prefix="binderjet-uia-")
            helper_path = Path(helper_directory.name) / "reader.ps1"
            helper_source = _HELPER or Path(__file__).with_name("native_helper.ps1").read_text(encoding="utf-8-sig")
            helper_path.write_text(helper_source, encoding="utf-8-sig")
            powershell = str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe")
            loader = "& ([scriptblock]::Create([IO.File]::ReadAllText('" + str(helper_path).replace("'", "''") + "')))"
            encoded_loader = base64.b64encode(loader.encode("utf-16-le")).decode("ascii")
            process = subprocess.Popen([powershell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Mta", "-EncodedCommand", encoded_loader],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       text=True, encoding="utf-8", errors="replace", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            with self._lock:
                connection_changed = expected_generation is not None and expected_generation != self._generation
                if not connection_changed:
                    self._active_helper = process
            if connection_changed:
                # The child is still waiting for stdin: no UI operation has
                # received a request, so disconnect cannot start a late Save.
                raise AdapterError("연결이 해제되어 이전 UI 작업을 취소했습니다.")
            deadline = time.monotonic() + deadline_seconds
            payload: str | None = json.dumps(request, ensure_ascii=False)
            while True:
                self._check_stopped(stop_event)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AdapterError(f"원본 UI 응답 제한 시간({deadline_seconds:g}초)을 초과했습니다.")
                try:
                    stdout, stderr = process.communicate(payload, timeout=min(0.05, remaining))
                    break
                except subprocess.TimeoutExpired:
                    payload = None
            try:
                response = json.loads(stdout.lstrip("\ufeff").strip())
            except (json.JSONDecodeError, TypeError) as exc:
                raise AdapterError("Windows UI 읽기 도우미가 올바른 응답을 반환하지 않았습니다. " + stderr[-300:]) from exc
            if not isinstance(response, dict) or not response.get("success", False):
                raise AdapterError(str(response.get("error", "Windows UI 읽기에 실패했습니다.")))
            return response
        finally:
            if process is not None:
                if process.poll() is None:
                    try:
                        process.kill()
                    except OSError:
                        pass
                try:
                    process.communicate(timeout=0.5)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            with self._lock:
                if self._active_helper is process:
                    self._active_helper = None
            self._helper_lock.release()
            if "helper_directory" in locals():
                helper_directory.cleanup()
