"""Bounded, isolated Excel-backed controller for user-selected .xlsx files.

No workbook data is ever saved. Formula support is deliberately limited to a
reviewable allowlist; the Excel application and calculation run in an owned
process, never in the BinderJet or an existing Excel instance.
"""
from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import re
import subprocess
import tempfile
import threading
import time
from types import MappingProxyType
import zipfile
from xml.etree import ElementTree as ET

from layer_algorithm import LayerRecommendation, number
from slice_data import FEATURE_NAMES
from output_limit import validate_maximum_ir

_CELL = re.compile(r"^\$?([A-Za-z]{1,3})\$?([1-9][0-9]{0,6})$")
_SAFE_FUNCTIONS = frozenset("IF IFERROR IFNA AND OR NOT TRUE FALSE ABS MIN MAX SUM AVERAGE COUNT COUNTA COUNTIF COUNTIFS SUMIF SUMIFS AVERAGEIF AVERAGEIFS INT ROUND ROUNDUP ROUNDDOWN SIGN MOD POWER SQRT ISNUMBER ISBLANK ISTEXT ISERROR ISNA CHOOSE MATCH INDEX LOOKUP VLOOKUP HLOOKUP XLOOKUP SUMPRODUCT MEDIAN LARGE SMALL FLOOR CEILING TRUNC EXACT LEN LEFT RIGHT MID CONCAT CONCATENATE TEXT VALUE N NA".split())
_PROHIBITED_PARTS = ("xl/externallinks/", "xl/querytables/", "xl/embeddings/", "xl/activex/", "xl/macrosheets/", "xl/dialogsheets/", "xl/vbaproject", "xl/connections.xml")
_MAX_BYTES = 16 * 1024 * 1024
_MAX_UNCOMPRESSED = 128 * 1024 * 1024
_MAX_ROWS = 10000
_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main", "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships", "p": "http://schemas.openxmlformats.org/package/2006/relationships"}


class _WorkbookRequestError(RuntimeError):
    def __init__(self, message, *, operation, elapsed, deadline, phase, process_state):
        super().__init__(message)
        self.operation = operation
        self.elapsed = elapsed
        self.deadline = deadline
        self.phase = phase
        self.process_state = process_state


class WorkbookTimeout(_WorkbookRequestError):
    """The response deadline expired; this worker must not be reused."""


class WorkbookTransportError(_WorkbookRequestError):
    """The worker or its transport failed before returning a response."""


def _address(value):
    match = _CELL.fullmatch(str(value).strip())
    if not match:
        raise ValueError("셀 주소는 B13 같은 단일 셀 주소여야 합니다.")
    col = 0
    for letter in match.group(1).upper():
        col = col * 26 + ord(letter) - 64
    row = int(match.group(2))
    if col > 16384 or row > 1048576:
        raise ValueError("Excel 셀 주소 범위를 벗어났습니다.")
    return col, row


@dataclass(frozen=True)
class WorkbookSpec:
    path: str
    sheet: str
    temperature_cell: str
    actual_power_cell: str
    recommendation_cell: str
    target_cell: str
    layer_cell: str | None = None
    slice_start_cell: str | None = None

    def slice_cells(self):
        if not self.slice_start_cell:
            return ()
        column, row = _address(self.slice_start_cell)
        result = []
        for name in FEATURE_NAMES:
            value, letters = column, ""
            if column > 16384:
                raise ValueError("슬라이싱 입력 열이 Excel 범위를 벗어났습니다.")
            while value:
                value, rem = divmod(value - 1, 26)
                letters = chr(65 + rem) + letters
            result.append((name, letters + str(row)))
            column += 1
        return tuple(result)

    def validate(self):
        if not self.path or Path(self.path).suffix.lower() != ".xlsx":
            raise ValueError("매크로 없는 .xlsx 파일을 선택하세요.")
        if len(self.sheet) > 31:
            raise ValueError("올바른 워크시트 이름을 입력하세요.")
        cells = [self.temperature_cell, self.actual_power_cell, self.recommendation_cell]
        if self.layer_cell:
            cells.append(self.layer_cell)
        cells.extend(address for _, address in self.slice_cells())
        coords = [_address(cell) for cell in cells]
        target = _address(self.target_cell)
        if len({col for col, _ in coords}) != len(coords):
            raise ValueError("층별 입력·출력 셀은 서로 다른 열이어야 합니다.")
        if len({row for _, row in coords}) != 1:
            raise ValueError("층별 입력·출력 시작 셀은 같은 행이어야 합니다.")
        if target[1] >= coords[0][1]:
            raise ValueError("목표 온도 셀은 층별 입력 시작 행 위에 있어야 합니다.")
        return coords, target


def _formula_safe(formula):
    # Ignore quoted literals and quoted sheet names while inspecting syntax.
    f = re.sub(r'"(?:[^"]|"")*"', '""', formula or "")
    f = re.sub(r"'(?:[^']|'')*'", "SHEET", f)
    if any(token in f for token in ("[", "]", "|", "@", "#", "{", "}")):
        raise ValueError("외부 참조·동적 배열·오류가 있는 수식은 사용할 수 없습니다.")
    for name in re.findall(r"(?<![A-Za-z0-9_.])([A-Za-z_][A-Za-z0-9_.]*)\s*\(", f):
        if name.upper() not in _SAFE_FUNCTIONS:
            raise ValueError("지원하지 않는 Excel 함수: " + name)


def _reject_future_reference(formula, current_row, sheet_name, first_row):
    """Historical rows are frozen, so references to future rows are unsafe."""
    f = re.sub(r'"(?:[^"]|"")*"', '""', formula or "")
    qualified = re.compile(r"('(?:[^']|'')+'|[A-Za-z_][A-Za-z0-9_.]*)!(\$?[A-Za-z]{1,3}\$?[1-9][0-9]{0,6})")
    def qualified_cell(match):
        name = match.group(1).strip("'").replace("''", "'")
        return match.group(2) if name.lower() == sheet_name.lower() else "0"
    f = qualified.sub(qualified_cell, f)
    for match in re.finditer(r"(?<![A-Za-z0-9_.])\$?[A-Za-z]{1,3}\$?([1-9][0-9]{0,6})(?![A-Za-z0-9_])", f):
        row = int(match.group(1))
        if row >= first_row and row > current_row:
            raise ValueError("미래 층을 참조하는 수식은 사용할 수 없습니다.")


def _reject_cross_sheet_history(formula, sheet_name, first_row, target):
    """Another sheet cannot depend on inputs or the changing target."""
    f = re.sub(r'"(?:[^"]|"")*"', '""', formula or "")
    qualified = re.compile(r"('(?:[^']|'')+'|[A-Za-z_][A-Za-z0-9_.]*)!(\$?[A-Za-z]{1,3}\$?[1-9][0-9]{0,6})(?::(\$?[A-Za-z]{1,3}\$?[1-9][0-9]{0,6}))?")
    for match in qualified.finditer(f):
        name = match.group(1).strip("'").replace("''", "'")
        if name.lower() == sheet_name.lower():
            a = _address(match.group(2))
            b = _address(match.group(3)) if match.group(3) else a
            covers_target = min(a[0], b[0]) <= target[0] <= max(a[0], b[0]) and min(a[1], b[1]) <= target[1] <= max(a[1], b[1])
            if max(a[1], b[1]) >= first_row or covers_target:
                raise ValueError("다른 시트가 목표 온도나 층별 영역을 참조하는 수식은 사용할 수 없습니다.")


def _referenced_sheets(formula):
    f = re.sub(r'"(?:[^"]|"")*"', '""', formula or "")
    qualified = re.compile(r"('(?:[^']|'')+'|[A-Za-z_][A-Za-z0-9_.]*)!(\$?[A-Za-z]{1,3}\$?[1-9][0-9]{0,6})")
    if re.search(r"[A-Za-z0-9_]:[A-Za-z0-9_]+!", f):
        raise ValueError("3차원 시트 참조는 사용할 수 없습니다.")
    names = {m.group(1).strip("'").replace("''", "'").lower() for m in qualified.finditer(f)}
    if "!" in qualified.sub("CELL", f):
        raise ValueError("확인할 수 없는 워크시트 참조입니다.")
    return names


def inspect_workbook(spec: WorkbookSpec, data: bytes, stop_event=None):
    """Validate the exact bytes used by Excel; never evaluate formulas here."""
    def check_cancelled():
        if stop_event is not None and stop_event.is_set():
            raise InterruptedError("연결이 취소되어 Excel 파일 검사를 중단했습니다.")
    check_cancelled()
    coords, target_coordinate = spec.validate()
    if len(data) > _MAX_BYTES:
        raise ValueError("Excel 파일은 16 MiB 이하여야 합니다.")
    try:
        with zipfile.ZipFile(BytesIO(data)) as z:
            infos = z.infolist()
            if len(infos) > 4096 or sum(i.file_size for i in infos) > _MAX_UNCOMPRESSED:
                raise ValueError("Excel 파일의 압축 해제 크기 또는 항목 수가 너무 큽니다.")
            names = {i.filename.lower() for i in infos}
            if len(names) != len(infos):
                raise ValueError("중복된 파일 항목이 있는 Excel 파일은 사용할 수 없습니다.")
            if any(any(name.startswith(prefix) for prefix in _PROHIBITED_PARTS) for name in names):
                raise ValueError("매크로·연결·외부 데이터가 있는 파일은 사용할 수 없습니다.")
            for info in infos:
                check_cancelled()
                if info.filename.lower().endswith(".rels"):
                    root = ET.fromstring(z.read(info))
                    if any(e.get("TargetMode", "").lower() == "external" for e in root):
                        raise ValueError("외부 링크가 있는 Excel 파일은 사용할 수 없습니다.")
            book = ET.fromstring(z.read("xl/workbook.xml"))
            for defined in book.findall("m:definedNames/m:definedName", _NS):
                if not (defined.get("name") or "").lower().startswith("_xlnm."):
                    raise ValueError("이름 정의를 포함한 Excel 파일은 사용할 수 없습니다.")
                _formula_safe(defined.text)
            sheets = book.findall("m:sheets/m:sheet", _NS)
            matches = [s for s in sheets if s.get("name") == spec.sheet] if spec.sheet else sheets[:1]
            if len(matches) != 1:
                raise ValueError("지정한 워크시트를 찾지 못했습니다.")
            relationship = matches[0].get("{" + _NS["r"] + "}id")
            rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
            paths = {}
            for item in sheets:
                rid = item.get("{" + _NS["r"] + "}id")
                destination = next((r.get("Target") for r in rels if r.get("Id") == rid), None)
                if destination:
                    path = destination.lstrip("/") if destination.startswith("/") else "xl/" + destination
                    paths[path.replace("\\", "/").lower()] = item.get("name").lower()
            target = next((r.get("Target") for r in rels if r.get("Id") == relationship), None)
            if not target:
                raise ValueError("워크시트 연결 정보가 없습니다.")
            sheet_path = target.lstrip("/") if target.startswith("/") else "xl/" + target
            sheet_path = sheet_path.replace("\\", "/")
            if ".." in sheet_path.split("/") or not sheet_path.lower().startswith("xl/worksheets/"):
                raise ValueError("올바르지 않은 워크시트 경로입니다.")
            sheet = ET.fromstring(z.read(sheet_path))
            cells = {}
            for c in sheet.findall(".//m:sheetData/m:row/m:c", _NS):
                if c.get("r"):
                    cells[c.get("r").upper()] = c
            def element_at(address):
                col, row = _address(address)
                letters = ""
                while col:
                    col, rem = divmod(col - 1, 26)
                    letters = chr(65 + rem) + letters
                return cells.get(letters + str(row))
            rec = element_at(spec.recommendation_cell)
            if rec is None or rec.find("m:f", _NS) is None:
                raise ValueError("권장 IR 시작 셀에 수식이 있어야 합니다.")
            for address in (spec.temperature_cell, spec.actual_power_cell, spec.layer_cell, spec.target_cell,
                            *(address for _, address in spec.slice_cells())):
                if address:
                    c = element_at(address)
                    if c is not None and c.find("m:f", _NS) is not None:
                        raise ValueError("입력 및 목표 셀에는 수식이 없어야 합니다: " + address)
            input_columns = {col for col, _ in coords}
            recommendation_column = _address(spec.recommendation_cell)[0]
            input_columns.remove(recommendation_column)
            max_row = coords[0][1]
            max_column = 1
            for position, (address, cell) in enumerate(cells.items()):
                if position % 256 == 0:
                    check_cancelled()
                try:
                    col, row = _address(address)
                    max_row = max(max_row, row)
                    max_column = max(max_column, col)
                except ValueError:
                    raise ValueError("워크시트에 잘못된 셀 주소가 있습니다.")
                formula = cell.find("m:f", _NS)
                if row >= coords[0][1] and formula is not None:
                    _reject_future_reference(formula.text, row, matches[0].get("name"), coords[0][1])
                value = cell.find("m:v", _NS)
                inline = cell.find("m:is", _NS)
                if row >= coords[0][1] and col in input_columns and formula is not None:
                    raise ValueError("층별 입력 열에는 수식이 없어야 합니다: " + address)
                if row >= coords[0][1] and col not in input_columns and formula is None and (
                    (value is not None and value.text not in (None, "")) or inline is not None
                ):
                    raise ValueError("지정하지 않은 층별 입력 값이 있습니다. 층 번호 셀을 지정하거나 해당 값을 제거하세요: " + address)
            if max_row - coords[0][1] + 1 > _MAX_ROWS:
                raise ValueError("층별 계산 영역은 10,000행 이하여야 합니다.")
            by_sheet = {}
            dependencies = {}
            for info in infos:
                check_cancelled()
                if info.filename.lower().startswith("xl/worksheets/") and info.filename.lower().endswith(".xml"):
                    root = ET.fromstring(z.read(info))
                    sheet_id = paths.get(info.filename.lower())
                    formulas = []
                    for position, formula in enumerate(root.findall(".//m:f", _NS)):
                        if position % 256 == 0:
                            check_cancelled()
                        if formula.get("t") in ("array", "dataTable"):
                            raise ValueError("배열 수식 및 데이터 표는 지원하지 않습니다.")
                        _formula_safe(formula.text)
                        refs = _referenced_sheets(formula.text)
                        if sheet_id:
                            dependencies.setdefault(sheet_id, set()).update(refs)
                            formulas.append(formula.text)
                    if sheet_id:
                        by_sheet[sheet_id] = formulas
            selected_name = matches[0].get("name").lower()
            reachable, pending = set(), [selected_name]
            while pending:
                current = pending.pop()
                if current in reachable:
                    continue
                if current not in by_sheet:
                    raise ValueError("참조한 워크시트를 확인할 수 없습니다: " + current)
                reachable.add(current)
                pending.extend(dependencies.get(current, set()) - reachable)
            for dependency in reachable - {selected_name}:
                for formula in by_sheet[dependency]:
                    _reject_cross_sheet_history(formula, matches[0].get("name"), coords[0][1], target_coordinate)
            return {"first_row": coords[0][1], "last_row": max_row, "max_column": max_column, "sheet": matches[0].get("name"), "sha256": hashlib.sha256(data).hexdigest()}
    except (zipfile.BadZipFile, KeyError, ET.ParseError) as error:
        raise ValueError("손상되었거나 지원하지 않는 .xlsx 파일입니다.") from error


@dataclass(frozen=True, init=False)
class PreparedWorkbook:
    spec: WorkbookSpec
    data: bytes
    metadata: object

    @classmethod
    def from_bytes(cls, spec, data, stop_event=None):
        data = bytes(data)
        metadata = inspect_workbook(spec, data, stop_event=stop_event)
        result = object.__new__(cls)
        object.__setattr__(result, 'spec', spec)
        object.__setattr__(result, 'data', data)
        object.__setattr__(result, 'metadata', MappingProxyType(dict(metadata)))
        return result

    @classmethod
    def from_file(cls, spec, stop_event=None):
        with open(spec.path, 'rb') as source:
            return cls.from_bytes(spec, source.read(_MAX_BYTES + 1), stop_event=stop_event)


class WorkbookController:
    def __init__(self, spec: WorkbookSpec, target_temperature_c: float, *, timeout=8.0, stop_event=None, maximum_ir_percent=100.0, on_created=None, prepared_workbook=None):
        self.spec = spec
        self.maximum_ir_percent = validate_maximum_ir(maximum_ir_percent)
        self.timeout = float(timeout)
        self.stop_event = stop_event
        if not number(target_temperature_c):
            raise ValueError("목표 온도는 유효한 숫자여야 합니다.")
        self.target = float(target_temperature_c)
        self._temp = None
        self._proc = None
        self._excel_handle = None
        self._lifecycle_lock = threading.Lock()
        self._closed = False
        self._queue = queue.Queue()
        self._count = 0
        self._last_layer = None
        self._phase = None
        self._last_operation = None
        self._operation_started = None
        self._retire_lock = threading.Lock()
        self._constructor_done = threading.Event()
        self._reader_done = threading.Event()
        self._reader_done.set()
        self._reader_thread = None
        self._excel_ownership_known = True
        self._excel_exited = True
        self._init_requested = False
        self._cleanup_active = False
        self._retirement_reason = None
        try:
            if on_created is not None:
                on_created(self)
            self._check_stopped()
            prepared = prepared_workbook or PreparedWorkbook.from_file(spec, stop_event=self.stop_event)
            if not isinstance(prepared, PreparedWorkbook) or prepared.spec != spec or hashlib.sha256(prepared.data).hexdigest() != prepared.metadata['sha256']:
                raise ValueError('Excel 파일과 검사 정보가 일치하지 않습니다.')
            data = prepared.data
            self.metadata = dict(prepared.metadata)
            if self.stop_event is not None and self.stop_event.is_set():
                raise InterruptedError("연결이 취소되어 Excel 계산을 시작하지 않았습니다.")
            temp = tempfile.TemporaryDirectory(prefix="IR-feedback-excel-")
            with self._lifecycle_lock:
                self._temp = temp
            self._check_stopped()
            private_path = Path(temp.name) / "workbook.xlsx"
            private_path.write_bytes(data)
            worker = Path(__file__).with_name("excel_worker.ps1")
            self._check_stopped()
            proc = subprocess.Popen(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(worker)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            with self._lifecycle_lock:
                self._proc = proc
                self._reader_done.clear()
                self._reader_thread = threading.Thread(target=self._reader, args=(proc,), daemon=True)
                self._reader_thread.start()
            self._check_stopped()
            request = dict(op="init", path=str(private_path), sheet=self.metadata["sheet"],
                           temperature=spec.temperature_cell, actual=spec.actual_power_cell,
                           recommendation=spec.recommendation_cell, target=spec.target_cell,
                           layer=spec.layer_cell, first=self.metadata["first_row"], last=self.metadata["last_row"],
                           max_column=self.metadata["max_column"], slice_inputs=dict(spec.slice_cells()))
            self._call(request, max(30.0, self.timeout))
        except Exception:
            self.close(force=True)
            raise
        finally:
            self._constructor_done.set()
            if self._closed:
                self.close(force=True)

    def _reader(self, proc):
        try:
            for line in proc.stdout:
                try:
                    data = json.loads(line)
                    if not isinstance(data, dict):
                        raise ValueError('Excel response is not an object')
                except Exception:
                    with self._lifecycle_lock:
                        if getattr(self, '_init_requested', False):
                            self._excel_ownership_known = False
                            self._excel_exited = False
                    self._queue.put({"error": "Excel 응답 형식이 올바르지 않습니다."})
                    continue
                if data.get("event") == "excel_starting":
                    with self._lifecycle_lock:
                        self._excel_ownership_known = False
                        self._excel_exited = False
                elif data.get("event") == "progress":
                    with self._lifecycle_lock:
                        self._phase = str(data.get("phase", ""))
                elif data.get("event") == "excel_started":
                    try:
                        pid = data.get("pid")
                        if type(pid) is not int or not 1 <= pid <= 0xFFFFFFFF:
                            raise ValueError("Invalid Excel process ID")
                        import ctypes
                        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
                        kernel.OpenProcess.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.c_uint]
                        kernel.OpenProcess.restype = ctypes.c_void_p
                        handle = kernel.OpenProcess(0x0001 | 0x1000 | 0x00100000, False, pid)
                        if not handle:
                            with self._lifecycle_lock:
                                self._excel_ownership_known = False
                                self._excel_exited = False
                            self._queue.put({"error": "전용 Excel 프로세스 관리 권한이 없습니다."})
                        else:
                            with self._lifecycle_lock:
                                self._excel_handle = handle
                                self._excel_ownership_known = True
                                self._excel_exited = False
                    except Exception:
                        with self._lifecycle_lock:
                            self._excel_ownership_known = False
                            self._excel_exited = False
                        self._queue.put({"error": "전용 Excel 프로세스를 확인할 수 없습니다."})
                else:
                    self._queue.put(data)
        finally:
            self._reader_done.set()
            self._queue.put({"transport_error": "Excel 계산 프로세스가 종료되었습니다."})

    def _check_stopped(self):
        if (self.stop_event is not None and self.stop_event.is_set()) or self._closed:
            self.close(force=True)
            raise InterruptedError("연결이 취소되어 Excel 계산을 중단했습니다.")

    def _call(self, command, timeout=None):
        started = time.monotonic()
        operation = str(command.get("op", "unknown"))
        # A current layer calculation has no elapsed-time validity cutoff.
        # Cancellation, process exit and protocol errors still terminate it.
        budget = None if operation == 'step' else (self.timeout if timeout is None else timeout)
        deadline = None if budget is None else started + budget
        with self._lifecycle_lock:
            self._last_operation = operation
            self._operation_started = started
            self._phase = "send"
            proc = self._proc
        def failure(cls, message):
            elapsed = time.monotonic() - started
            with self._lifecycle_lock:
                phase = self._phase
            state = "missing" if proc is None else ("running" if proc.poll() is None else "exited")
            self.close(force=True)
            return cls(f"{message} ({operation}, {phase}, {elapsed:.3f}초)", operation=operation,
                       elapsed=elapsed, deadline=budget, phase=phase, process_state=state)
        self._check_stopped()
        if not proc or proc.poll() is not None:
            raise failure(WorkbookTransportError, "Excel 계산 프로세스가 실행 중이 아닙니다.")
        try:
            if operation == 'init':
                with self._lifecycle_lock:
                    self._init_requested = True
            proc.stdin.write(json.dumps(command, ensure_ascii=True) + "\n")
            proc.stdin.flush()
            while True:
                self._check_stopped()
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    raise queue.Empty
                try:
                    response = self._queue.get(timeout=0.1 if remaining is None else min(remaining, 0.1))
                    self._check_stopped()
                    if deadline is not None and time.monotonic() >= deadline:
                        raise queue.Empty
                    break
                except queue.Empty:
                    continue
        except InterruptedError:
            raise
        except queue.Empty as error:
            raise failure(WorkbookTimeout, "Excel 계산 응답이 지연되어 제어를 중단했습니다.") from error
        except (OSError, ValueError) as error:
            self._check_stopped()
            raise failure(WorkbookTransportError, "Excel 계산 프로세스와 통신할 수 없습니다.") from error
        if response.get("transport_error"):
            raise failure(WorkbookTransportError, response["transport_error"])
        if response.get("error"):
            self.close(force=True)
            raise RuntimeError("Excel 계산 실패: " + str(response["error"]))
        return response

    def set_target_temperature(self, value):
        if not number(value):
            raise ValueError("목표 온도는 유효한 숫자여야 합니다.")
        self.target = float(value)

    def reset(self):
        if self._count:
            self._call({"op": "reset"}, max(30.0, getattr(self, "timeout", 8.0)))
        self._count = 0
        self._last_layer = None

    def process(self, layer, temperature_c, previous_actual_power, restart=False, *, slice_features=None):
        if not isinstance(layer, int) or isinstance(layer, bool) or layer < 1:
            raise ValueError("층 번호는 양의 정수여야 합니다.")
        if not number(temperature_c) or not number(previous_actual_power) or not 0 <= previous_actual_power <= self.maximum_ir_percent:
            raise ValueError("온도 또는 현재 IR 출력이 올바르지 않습니다.")
        if restart or (self._last_layer is not None and layer != self._last_layer + 1):
            self.reset()
        if self.metadata["first_row"] + self._count > self.metadata["last_row"]:
            raise ValueError("Excel 수식이 준비된 층 수를 초과했습니다.")
        request = dict(op="step", index=self._count, layer=layer,
                       temperature=temperature_c, actual=previous_actual_power, target=self.target)
        spec = getattr(self, 'spec', None)
        if spec is not None and spec.slice_start_cell:
            values = dict.fromkeys(FEATURE_NAMES)
            values.update(image_valid=0, previous_image_valid=0, previous_powder_layers=0)
            if slice_features is not None:
                if set(slice_features) != set(FEATURE_NAMES):
                    raise ValueError("슬라이싱 입력 항목이 일치하지 않습니다.")
                values.update(slice_features)
            for name, value in values.items():
                if value is not None and not number(value):
                    raise ValueError("슬라이싱 입력값이 올바르지 않습니다: " + name)
                if value is None:
                    continue
                if name.endswith('_pct') and not 0 <= value <= 100:
                    raise ValueError("슬라이싱 비율은 0~100이어야 합니다.")
                if name in ('image_valid', 'previous_image_valid') and value not in (0, 1):
                    raise ValueError("이미지 유효성은 0 또는 1이어야 합니다.")
                if name == 'previous_powder_layers' and (type(value) is not int or value < 0):
                    raise ValueError("무분사 참고 층 수가 올바르지 않습니다.")
                if name in ('radius_mm', 'dpi_x', 'dpi_y') and value <= 0:
                    raise ValueError("반경과 DPI는 양수여야 합니다.")
            for prefix in ('', 'previous_'):
                fields = ('center_coverage_pct', 'center_intensity_pct', 'whole_coverage_pct')
                present = [values[prefix + name] is not None for name in fields]
                if values[prefix + 'image_valid'] not in (0, 1) or (all(present) != bool(values[prefix + 'image_valid'])) or (any(present) and not all(present)):
                    raise ValueError("누락 이미지와 수치의 유효성이 일치하지 않습니다.")
            request['image'] = values
        response = self._call(request)
        value = response.get("value")
        if not number(value) or not 0 <= value <= 100:
            raise ValueError("권장 IR 수식의 결과가 0~100 범위의 숫자가 아닙니다.")
        safe = max(0.0, min(self.maximum_ir_percent, previous_actual_power + 5.0, max(previous_actual_power - 5.0, float(value))))
        self._count += 1
        self._last_layer = layer
        return LayerRecommendation(safe, "Excel 수식" if safe == value else "Excel 수식 · 최대 출력/증감 제한", {"excel_raw": value, "maximum_ir_percent": self.maximum_ir_percent})

    def retirement_status(self):
        if not self._lifecycle_lock.acquire(blocking=False):
            return dict(complete=False, verified=False, reason='lifecycle state busy',
                        worker_exited=False, reader_done=False, excel_ownership_known=False,
                        excel_exited=False, temp_deleted=False)
        try:
            proc = self._proc
            worker_exited = proc is None or proc.poll() is not None
            reader_done = getattr(self, '_reader_done', None)
            reader_done = reader_done is None or reader_done.is_set()
            constructor = getattr(self, '_constructor_done', None)
            constructor_done = constructor is None or constructor.is_set()
            ownership = getattr(self, '_excel_ownership_known', True)
            excel_exited = getattr(self, '_excel_exited', self._excel_handle is None)
            temp_deleted = self._temp is None
            complete = self._closed and constructor_done and not getattr(self, '_cleanup_active', False)
            verified = complete and worker_exited and reader_done and ownership and excel_exited and temp_deleted
            reason = getattr(self, '_retirement_reason', None)
            if not verified and not reason:
                reason = ('constructor active' if not constructor_done else
                          'cleanup in progress' if not complete else
                          'worker exit unconfirmed' if not worker_exited else
                          'reader not finished' if not reader_done else
                          'Excel ownership unknown' if not ownership else
                          'Excel exit unconfirmed' if not excel_exited else 'temporary files remain')
            return dict(complete=complete, verified=verified, reason=None if verified else reason,
                        worker_exited=worker_exited, reader_done=reader_done,
                        excel_ownership_known=ownership, excel_exited=excel_exited,
                        temp_deleted=temp_deleted)
        finally:
            self._lifecycle_lock.release()

    def close(self, force=False):
        # A separate lock serializes retirement attempts. Never hold the
        # lifecycle lock while waiting for a process or the reader thread.
        retire_lock = getattr(self, '_retire_lock', None)
        if retire_lock is None:  # lightweight test fixtures created without __init__
            retire_lock = self._retire_lock = threading.Lock()
        with retire_lock:
            with self._lifecycle_lock:
                self._closed = True
                self._cleanup_active = True
                self._retirement_reason = None
                proc = self._proc
                reader = getattr(self, '_reader_thread', None)
            reasons = []
            try:
                if proc is not None:
                    try:
                        if not force and proc.poll() is None:
                            proc.stdin.write('{"op":"close"}\n')
                            proc.stdin.flush()
                            proc.wait(timeout=2)
                        if proc.poll() is None:
                            proc.kill()
                        proc.wait(timeout=2)
                        if proc.poll() is not None:
                            with self._lifecycle_lock:
                                if self._proc is proc:
                                    self._proc = None
                        else:
                            reasons.append('worker exit unconfirmed')
                    except Exception:
                        reasons.append('worker exit unconfirmed')
                if reader is not None and reader is not threading.current_thread():
                    reader.join(timeout=2)
                if reader is not None and not self._reader_done.is_set():
                    reasons.append('reader not finished')
                # Drain stdout before inspecting the handle: excel_started can
                # arrive after close begins, and its process must be retired too.
                with self._lifecycle_lock:
                    handle = self._excel_handle
                if handle:
                    try:
                        import ctypes
                        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
                        kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint]
                        kernel.WaitForSingleObject.restype = ctypes.c_uint
                        kernel.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint]
                        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
                        if kernel.WaitForSingleObject(handle, 0) != 0:
                            kernel.TerminateProcess(handle, 1)
                        if kernel.WaitForSingleObject(handle, 2000) != 0:
                            reasons.append('Excel exit unconfirmed')
                        elif not kernel.CloseHandle(handle):
                            reasons.append('Excel handle close failed')
                        else:
                            with self._lifecycle_lock:
                                if self._excel_handle == handle:
                                    self._excel_handle = None
                                    self._excel_exited = True
                    except Exception:
                        reasons.append('Excel exit unconfirmed')
                with self._lifecycle_lock:
                    temp = self._temp
                if temp is not None:
                    try:
                        temp.cleanup()
                        with self._lifecycle_lock:
                            if self._temp is temp:
                                self._temp = None
                    except Exception:
                        reasons.append('temporary file cleanup failed')
            finally:
                with self._lifecycle_lock:
                    self._retirement_reason = '; '.join(reasons) or None
                    self._cleanup_active = False
