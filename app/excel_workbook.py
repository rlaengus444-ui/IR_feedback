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
import zipfile
from xml.etree import ElementTree as ET

from layer_algorithm import LayerRecommendation, number

_CELL = re.compile(r"^\$?([A-Za-z]{1,3})\$?([1-9][0-9]{0,6})$")
_SAFE_FUNCTIONS = frozenset("IF IFERROR IFNA AND OR NOT TRUE FALSE ABS MIN MAX SUM AVERAGE COUNT COUNTA COUNTIF COUNTIFS SUMIF SUMIFS AVERAGEIF AVERAGEIFS INT ROUND ROUNDUP ROUNDDOWN SIGN MOD POWER SQRT ISNUMBER ISBLANK ISTEXT ISERROR ISNA CHOOSE MATCH INDEX LOOKUP VLOOKUP HLOOKUP XLOOKUP SUMPRODUCT MEDIAN LARGE SMALL FLOOR CEILING TRUNC EXACT LEN LEFT RIGHT MID CONCAT CONCATENATE TEXT VALUE N NA".split())
_PROHIBITED_PARTS = ("xl/externallinks/", "xl/querytables/", "xl/embeddings/", "xl/activex/", "xl/macrosheets/", "xl/dialogsheets/", "xl/vbaproject", "xl/connections.xml")
_MAX_BYTES = 16 * 1024 * 1024
_MAX_UNCOMPRESSED = 128 * 1024 * 1024
_MAX_ROWS = 10000
_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main", "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships", "p": "http://schemas.openxmlformats.org/package/2006/relationships"}


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

    def validate(self):
        if not self.path or Path(self.path).suffix.lower() != ".xlsx":
            raise ValueError("매크로 없는 .xlsx 파일을 선택하세요.")
        if len(self.sheet) > 31:
            raise ValueError("올바른 워크시트 이름을 입력하세요.")
        cells = [self.temperature_cell, self.actual_power_cell, self.recommendation_cell]
        if self.layer_cell:
            cells.append(self.layer_cell)
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
            for address in (spec.temperature_cell, spec.actual_power_cell, spec.layer_cell, spec.target_cell):
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


class WorkbookController:
    def __init__(self, spec: WorkbookSpec, target_temperature_c: float, *, timeout=8.0, stop_event=None):
        self.spec = spec
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
        try:
            with open(spec.path, "rb") as f:
                data = f.read(_MAX_BYTES + 1)
            self.metadata = inspect_workbook(spec, data, stop_event=self.stop_event)
            if self.stop_event is not None and self.stop_event.is_set():
                raise InterruptedError("연결이 취소되어 Excel 계산을 시작하지 않았습니다.")
            self._temp = tempfile.TemporaryDirectory(prefix="IR-feedback-excel-")
            private_path = Path(self._temp.name) / "workbook.xlsx"
            private_path.write_bytes(data)
            worker = Path(__file__).with_name("excel_worker.ps1")
            self._proc = subprocess.Popen(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(worker)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            threading.Thread(target=self._reader, args=(self._proc,), daemon=True).start()
            request = dict(op="init", path=str(private_path), sheet=self.metadata["sheet"],
                           temperature=spec.temperature_cell, actual=spec.actual_power_cell,
                           recommendation=spec.recommendation_cell, target=spec.target_cell,
                           layer=spec.layer_cell, first=self.metadata["first_row"], last=self.metadata["last_row"],
                           max_column=self.metadata["max_column"])
            self._call(request, max(30.0, self.timeout))
        except Exception:
            self.close(force=True)
            raise

    def _reader(self, proc):
        try:
            for line in proc.stdout:
                try:
                    data = json.loads(line)
                except Exception:
                    self._queue.put({"error": "Excel 응답 형식이 올바르지 않습니다."})
                    continue
                if data.get("event") == "excel_started":
                    try:
                        import ctypes
                        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
                        kernel.OpenProcess.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.c_uint]
                        kernel.OpenProcess.restype = ctypes.c_void_p
                        handle = kernel.OpenProcess(0x0001 | 0x1000, False, int(data["pid"]))
                        if not handle:
                            self._queue.put({"error": "전용 Excel 프로세스 관리 권한이 없습니다."})
                        else:
                            with self._lifecycle_lock:
                                if self._closed:
                                    kernel.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint]
                                    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
                                    kernel.TerminateProcess(handle, 1)
                                    kernel.CloseHandle(handle)
                                else:
                                    self._excel_handle = handle
                    except Exception:
                        self._queue.put({"error": "전용 Excel 프로세스를 확인할 수 없습니다."})
                else:
                    self._queue.put(data)
        finally:
            self._queue.put({"error": "Excel 계산 프로세스가 종료되었습니다."})

    def _call(self, command, timeout=None):
        if not self._proc or self._proc.poll() is not None:
            raise RuntimeError("Excel 계산 프로세스가 실행 중이 아닙니다.")
        try:
            self._proc.stdin.write(json.dumps(command, ensure_ascii=True) + "\n")
            self._proc.stdin.flush()
            deadline = time.monotonic() + (timeout or self.timeout)
            while True:
                if self.stop_event is not None and self.stop_event.is_set():
                    self.close(force=True)
                    raise InterruptedError("연결이 취소되어 Excel 계산을 중단했습니다.")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise queue.Empty
                try:
                    response = self._queue.get(timeout=min(remaining, 0.1))
                    break
                except queue.Empty:
                    continue
        except InterruptedError:
            raise
        except (OSError, BrokenPipeError, queue.Empty) as error:
            self.close(force=True)
            raise RuntimeError("Excel 계산 응답이 지연되어 제어를 중단했습니다.") from error
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
            self._call({"op": "reset"})
        self._count = 0
        self._last_layer = None

    def process(self, layer, temperature_c, previous_actual_power, restart=False):
        if not isinstance(layer, int) or isinstance(layer, bool) or layer < 1:
            raise ValueError("층 번호는 양의 정수여야 합니다.")
        if not number(temperature_c) or not number(previous_actual_power) or not 0 <= previous_actual_power <= 100:
            raise ValueError("온도 또는 현재 IR 출력이 올바르지 않습니다.")
        if restart or (self._last_layer is not None and layer != self._last_layer + 1):
            self.reset()
        if self.metadata["first_row"] + self._count > self.metadata["last_row"]:
            raise ValueError("Excel 수식이 준비된 층 수를 초과했습니다.")
        response = self._call(dict(op="step", index=self._count, layer=layer,
                                   temperature=temperature_c, actual=previous_actual_power, target=self.target))
        value = response.get("value")
        if not number(value) or not 0 <= value <= 100:
            raise ValueError("권장 IR 수식의 결과가 0~100 범위의 숫자가 아닙니다.")
        safe = max(0.0, min(100.0, previous_actual_power + 5.0, max(previous_actual_power - 5.0, float(value))))
        self._count += 1
        self._last_layer = layer
        return LayerRecommendation(safe, "Excel 수식" if safe == value else "Excel 수식 · 최대 증감 제한", {"excel_raw": value})

    def close(self, force=False):
        with self._lifecycle_lock:
            self._closed = True
            excel_handle = self._excel_handle
            self._excel_handle = None
        proc = self._proc
        self._proc = None
        if proc:
            if not force and proc.poll() is None:
                try:
                    proc.stdin.write('{"op":"close"}\n')
                    proc.stdin.flush()
                    proc.wait(timeout=2)
                except Exception:
                    pass
            if proc.poll() is None:
                proc.kill()
                try:
                    proc.wait(timeout=2)
                except Exception:
                    pass
        if excel_handle:
            import ctypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint]
            kernel.CloseHandle.argtypes = [ctypes.c_void_p]
            # An open process handle cannot be redirected by PID reuse. The
            # worker only supplies a PID absent before its COM creation.
            kernel.TerminateProcess(excel_handle, 1)
            kernel.CloseHandle(excel_handle)
        if self._temp:
            try:
                self._temp.cleanup()
            except OSError:
                pass
            self._temp = None
