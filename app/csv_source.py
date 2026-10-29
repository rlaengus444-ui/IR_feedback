"""Read the existing completed-layer CSV without calling any target UI API."""
from __future__ import annotations

from contextlib import contextmanager
import csv
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
import hashlib
import io
import itertools
import math
import os
from pathlib import Path
import re
import time

try:
    from .adapter_contracts import AdapterError, MachineWindow
except ImportError:
    from adapter_contracts import AdapterError, MachineWindow


class CsvSourceError(AdapterError):
    pass


@contextmanager
def shared_binary(path: Path):
    """The reader must not prevent the printer from appending or replacing files."""
    if os.name != "nt":
        with path.open("rb", buffering=0) as stream:
            yield stream
        return
    import ctypes
    from ctypes import wintypes
    import msvcrt
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateFileW(str(path), 0x80000000, 7, None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        kernel.CloseHandle(handle)
        raise
    with os.fdopen(descriptor, "rb", buffering=0) as stream:
        yield stream


def monitoring_roots() -> tuple[Path, ...]:
    roots = []
    override = os.environ.get("BINDERJET_UNIFIED_MONITORING_ROOT", "").strip()
    if override:
        roots.append(Path(override))
    desktop = Path.home() / "Desktop"
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        class Guid(ctypes.Structure):
            _fields_ = [("data1", wintypes.DWORD), ("data2", wintypes.WORD),
                        ("data3", wintypes.WORD), ("data4", ctypes.c_ubyte * 8)]
        # FOLDERID_Desktop, the same known folder used by Environment.SpecialFolder.
        value = Guid(0xB4BFCC3A, 0xDB2C, 0x424C, (ctypes.c_ubyte * 8)(0xB0,0x29,0x7F,0xE9,0x9A,0x87,0xC6,0x41))
        shell = ctypes.WinDLL("shell32")
        shell.SHGetKnownFolderPath.argtypes = [ctypes.POINTER(Guid), wintypes.DWORD, wintypes.HANDLE, ctypes.POINTER(ctypes.c_void_p)]
        shell.SHGetKnownFolderPath.restype = ctypes.c_long
        ole = ctypes.WinDLL("ole32")
        ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]
        pointer = ctypes.c_void_p()
        if shell.SHGetKnownFolderPath(ctypes.byref(value), 0, None, ctypes.byref(pointer)) == 0:
            try: desktop = Path(ctypes.wstring_at(pointer))
            finally: ole.CoTaskMemFree(pointer)
    roots.append(desktop / "Monitoring")
    return tuple(dict.fromkeys(path.resolve() for path in roots))


def log_timestamp(line: str) -> datetime | None:
    match = re.match(r"^(\d{4}-\d{2}-\d{2})([^\d]*)(\d{2})(\d{2})(\d{2})\.\.\.", line.strip())
    if not match:
        return None
    date, meridiem, hour, minute, second = match.groups()
    hour = int(hour)
    meridiem = meridiem.strip().casefold()
    if meridiem in {"오전", "am", "上午", "a.m."}:
        hour %= 12
    elif meridiem in {"오후", "pm", "下午", "p.m."}:
        hour = hour % 12 + 12
    elif meridiem:
        return None
    try: return datetime.fromisoformat(date).replace(hour=hour, minute=int(minute), second=int(second))
    except ValueError: return None


@dataclass(frozen=True)
class JobContext:
    run_key: str | None = None
    first_layer: int | None = None
    first_started: datetime | None = None
    current_layer: int | None = None
    state: str = "unknown"


class JobLog:
    MAX_INITIAL_BYTES = 8 * 1024 * 1024
    MAX_APPEND_BYTES = 1024 * 1024

    def __init__(self, target: MachineWindow):
        self.target = target
        self.context = JobContext()
        self.path = None
        self.identity = None
        self.offset = 0
        self.pending = b""
        self.last_stat = None
        self.window_start = 0
        self.window_hash = None

    def read(self) -> JobContext:
        if self.target.machine not in {"M1", "S3"} or not self.target.image_path:
            return self.context
        folder = Path(self.target.image_path).parent / "Logs"
        try:
            with os.scandir(folder) as entries:
                candidates = [Path(item.path) for item in itertools.islice(entries, 4096)
                              if item.is_file() and item.name.casefold().endswith("_" + self.target.machine.casefold() + ".log")]
            if not candidates:
                return self.context
            path = max(candidates, key=lambda item: item.stat().st_mtime_ns)
            with shared_binary(path) as stream:
                stat = os.fstat(stream.fileno())
                identity = (stat.st_dev, stat.st_ino)
                signature = (identity, stat.st_size, stat.st_mtime_ns)
                reset = path != self.path or identity != self.identity or stat.st_size < self.offset
                # Recheck the previously inspected window even when metadata is unchanged.
                old = None
                old_start = self.window_start
                if not reset and self.window_hash is not None:
                    stream.seek(self.window_start)
                    old = stream.read(self.offset - self.window_start)
                    if hashlib.sha256(old).digest() != self.window_hash:
                        reset = True
                if reset:
                    self.context = JobContext()
                    self.offset = max(0, stat.st_size - self.MAX_INITIAL_BYTES)
                    self.pending = b""
                    self.window_start = self.offset
                end = min(stat.st_size, self.offset + (self.MAX_INITIAL_BYTES if reset else self.MAX_APPEND_BYTES))
                # At most the last 8 MiB of consumed log is retained as identity.
                start = max(self.window_start, end - self.MAX_INITIAL_BYTES)
                stream.seek(start)
                first = stream.read(end - start)
                stream.seek(start)
                second = stream.read(end - start)
                after = os.fstat(stream.fileno())
                current = path.stat()
                overlap_changed = old is not None and not reset and first[:self.offset - start] != old[start - old_start:]
                if (overlap_changed or first != second or
                    (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns) or
                    (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns) != (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)):
                    self.context = JobContext()
                    self.path, self.identity, self.window_hash = None, None, None
                    return self.context
                raw = first[self.offset - start:]
                self.offset = end
                self.window_start, self.window_hash = start, hashlib.sha256(first).digest()
                self.path, self.identity, self.last_stat = path, identity, signature
            lines = (self.pending + raw).split(b"\n")
            self.pending = lines.pop()
            if len(self.pending) > 65536:
                raise CsvSourceError("원본 로그의 한 줄이 허용 크기를 초과했습니다.")
            for raw_line in lines:
                line = raw_line.decode("utf-8-sig", errors="replace").strip()
                if "AUTO" not in line:
                    continue
                if "AutoPrint started!" in line:
                    stamp = log_timestamp(line)
                    key = hashlib.sha256((str(path) + "\0" + line).encode()).hexdigest() if stamp else None
                    self.context = JobContext(run_key=key, state="AUTO")
                match = re.search(r"\bLayer:\s*(\d+)\s*->\s*Current Z\s*=", line)
                if match and self.context.run_key:
                    layer = int(match.group(1))
                    if not 0 <= layer <= 1000000:
                        raise CsvSourceError("원본 로그의 층 번호가 허용 범위를 벗어났습니다.")
                    first = self.context.first_layer
                    self.context = replace(self.context, current_layer=layer,
                        first_layer=layer if first is None else first,
                        first_started=log_timestamp(line) if first is None else self.context.first_started)
                if any(text in line for text in ("AutoPrint finished!", "AutoPrint cancelled.", "Stopping printing as requested.")):
                    self.context = replace(self.context, state="IDLE")
            return self.context
        except (OSError, UnicodeError):
            self.context = JobContext()
            self.path, self.identity, self.window_hash = None, None, None
            return self.context


RECORD_TIME_FORMATS = ("%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f")


def record_time(text: str) -> datetime | None:
    for pattern in RECORD_TIME_FORMATS:
        try:
            value = datetime.strptime(text.strip(), pattern)
            return value if value.year >= 2000 else None
        except ValueError: pass
    return None


def unset_record_time(text: str) -> bool:
    """Recognize only blank or the original writer's DateTime.MinValue."""
    if not text.strip():
        return True
    for pattern in RECORD_TIME_FORMATS:
        try:
            return datetime.strptime(text.strip(), pattern) == datetime.min
        except ValueError:
            pass
    return False


@dataclass(frozen=True)
class CsvSample:
    layer_index: int
    temperature: float
    start: str
    finished: datetime
    delay_seconds: float | None
    key: str
    slicing_image_path: str = ""
    recoating_finished: datetime | None = None
    slice_lineage: str = ""
    source_generation: str = ""


@dataclass(frozen=True)
class CsvSnapshot:
    sample: CsvSample | None
    context: JobContext
    token: dict | None
    is_new: bool
    details: str


class MonitoringCsvReader:
    MAX_FILE_BYTES = 32 * 1024 * 1024
    MAX_ROW_BYTES = 65536
    POLL_SECONDS = 0.25
    MAX_NEW_AGE_SECONDS = 5.0
    REQUIRED = ("Id", "TimeStart", "TimeRecoatingFinished", "TimePrintingFinished", "TemperaturePostRecoating")

    def __init__(self, target: MachineWindow, *, roots=None, log=None, clock=time.time):
        self.target = target
        self.roots = tuple(Path(root).resolve() for root in roots) if roots is not None else monitoring_roots()
        self.log = log or JobLog(target)
        self.clock = clock
        self.connected_at = clock()
        self.path = None
        self.identity = None
        self.run_key = None
        self.offset = 0
        self.pending = b""
        self.header = None
        self.latest = None
        self.last_signature = None
        self._last_scan = 0.0
        self.generation = 0
        self.prefix_hash = None
        self._history_prefix_open = True
        self._skipped_history_rows = 0
        self._resume_anchor_start = self._resume_anchor_finish = None
        self._resume_baseline_pending = False

    def _select(self, context: JobContext):
        if context.first_started is None or context.first_layer is None:
            return None
        names = {"LayerDatasheet_" + (context.first_started + timedelta(seconds=delta)).strftime("%Y%m%d_%H%M%S") + ".csv" for delta in (-2,-1,0,1,2)}
        found = []
        for root in self.roots:
            if not root.is_dir(): continue
            with os.scandir(root) as entries:
                for entry in itertools.islice(entries, 4096):
                    if not entry.is_dir(follow_symlinks=False): continue
                    directory = Path(entry.path)
                    if not re.fullmatch(r"\d{8}_\d{6}_\d+-layer", directory.name): continue
                    for name in names:
                        path = directory / name
                        if path.is_file() and not path.is_symlink(): found.append(path)
        if len(found) > 1:
            raise CsvSourceError("현재 작업의 Monitoring CSV 후보가 여러 개여서 자동 선택하지 않았습니다.")
        return found[0] if found else None

    def _clear_file(self):
        self.identity, self.header, self.latest, self.last_signature = None, None, None, None
        self.offset, self.pending = 0, b""
        self.prefix_hash = None
        self._history_prefix_open = True
        self._skipped_history_rows = 0
        self._resume_anchor_start = self._resume_anchor_finish = None
        self._resume_baseline_pending = False

    def _context_error(self, context, index, start, finish, reason):
        return CsvSourceError(
            "CSV 행이 현재 작업의 시작 시점/층과 일치하지 않습니다. "
            f"[원인={reason}; CSV Id={index + 1}(내부 층={index}); "
            f"작업 시작 내부 층={context.first_layer}; "
            f"행 시작={start.isoformat(sep=' ') if start else '미기록/해석 불가'}; "
            f"행 완료={finish.isoformat(sep=' ') if finish else '미기록/해석 불가'}; "
            f"로그 시작={context.first_started.isoformat(sep=' ')}]")

    def _resume_anchor(self, context, index, start, finish, raw_finish):
        """Bind retained history to this run without treating metadata as a reading."""
        if (start is None or abs((start - context.first_started).total_seconds()) > 2 or
                (finish is None and not unset_record_time(raw_finish))):
            raise self._context_error(context, index, start, finish, "재시작 시작 행의 시각이 없거나 일치하지 않음")
        if self._resume_anchor_start is not None:
            if (start != self._resume_anchor_start or
                    (finish is not None and self._resume_anchor_finish is not None and
                     finish != self._resume_anchor_finish)):
                raise self._context_error(context, index, start, finish, "중복된 재시작 시작 행의 시각이 충돌")
        else:
            self._resume_anchor_start = start
            # This latch survives partial tails and parser chunks. It is only
            # consumed when a stable valid measurement and full token return.
            self._resume_baseline_pending = True
        if finish is not None:
            self._resume_anchor_finish = finish

    def _sample(self, row: list[str], context: JobContext) -> CsvSample | None:
        if len(row) != len(self.header):
            raise CsvSourceError("Monitoring CSV 열 수가 헤더와 일치하지 않습니다.")
        data = dict(zip(self.header, row))
        try:
            index = int(data["Id"]) - 1
            temperature = float(data["TemperaturePostRecoating"])
        except ValueError as exc:
            raise CsvSourceError("Monitoring CSV의 층/온도 형식이 올바르지 않습니다.") from exc
        if not 0 <= index <= 1000000 or not math.isfinite(temperature) or not -273.15 <= temperature <= 2000:
            raise CsvSourceError("Monitoring CSV의 층/온도가 허용 범위를 벗어났습니다.")
        start, finish = record_time(data["TimeStart"]), record_time(data["TimePrintingFinished"])
        # The printer records -1 when its thermometer is disabled. A completed
        # row carrying that sentinel is not a temperature measurement and must
        # stop control, rather than be learned as a valid low temperature.
        if temperature == -1:
            raise CsvSourceError("온도계 비활성 값(-1°C)이 기록되어 IR 제어를 중단합니다.")
        if start is not None and finish is not None and finish < start:
            raise self._context_error(context, index, start, finish, "완료 시각이 시작 시각보다 빠름")
        if (self._resume_anchor_start is not None and index < context.first_layer and
                start is not None and finish is not None):
            # A zero thermometer value is not a measurement, but valid start
            # and finish timestamps still identify a completed older row.
            raise self._context_error(context, index, start, finish, "재시작 시작 행 뒤에 이전 완료 층이 나타남")
        if self._skipped_history_rows and index == context.first_layer:
            self._resume_anchor(context, index, start, finish, data["TimePrintingFinished"])
        if temperature == 0 or start is None or finish is None:
            return None  # Metadata above never becomes a temperature sample.
        earliest = context.first_started - timedelta(seconds=2)
        if index < context.first_layer:
            # Resuming the original job can retain completed rows from its
            # previous run. Only a leading, strictly older prefix is history.
            # No such row supplies a measurement, token, or control proposal.
            if (self._history_prefix_open and self._resume_anchor_start is None and
                    start < earliest and finish < earliest):
                self._skipped_history_rows += 1
                return None
            raise self._context_error(context, index, start, finish,
                                      "이전 층 행의 시간대 또는 위치가 현재 작업과 충돌")
        if start < earliest:
            raise self._context_error(context, index, start, finish, "현재 층 행이 작업 시작보다 오래됨")
        if self._history_prefix_open and self._skipped_history_rows:
            # Skipping history adds an exception to normal membership checks;
            # an exact metadata anchor may precede the first usable reading.
            if self._resume_anchor_start is None:
                raise self._context_error(context, index, start, finish, "재시작 기준 층/시각을 확인할 수 없음")
        self._history_prefix_open = False
        recoating = record_time(data["TimeRecoatingFinished"])
        delay = (finish - recoating).total_seconds() if recoating and start <= recoating <= finish else None
        identity = [str(self.path), self.identity, self.generation, context.run_key, index, data["TimeStart"], temperature]
        key = hashlib.sha256(repr(identity).encode()).hexdigest()
        # Optional lineage never relaxes ordinary CSV temperature/job validation.
        # Its separate token is checked only by explicitly enabled auto images.
        image_path = data.get("ImgSlicing", "")
        lineage = hashlib.sha256(repr((image_path, data["TimeRecoatingFinished"])).encode()).hexdigest()
        return CsvSample(index, temperature, data["TimeStart"], finish, delay, key,
                         image_path, recoating if delay is not None else None, lineage,
                         hashlib.sha256(repr(identity[:4]).encode()).hexdigest())

    def read(self) -> CsvSnapshot:
        context = self.log.read()
        if context.run_key != self.run_key:
            self.run_key = context.run_key
            self.path = None
            self._clear_file()
        if self.path is None:
            if time.monotonic() - self._last_scan < 1:
                return CsvSnapshot(None, context, None, False, "현재 작업의 CSV 생성 대기")
            self._last_scan = time.monotonic()
            self.path = self._select(context)
        if self.path is None:
            return CsvSnapshot(None, context, None, False, "작업 시작 로그와 일치하는 Monitoring CSV 생성 대기")
        try:
            with shared_binary(self.path) as stream:
                before = os.fstat(stream.fileno())
                if before.st_size > self.MAX_FILE_BYTES:
                    raise CsvSourceError("Monitoring CSV가 32 MiB 읽기 상한을 초과했습니다.")
                identity = (before.st_dev, before.st_ino)
                signature = (identity, before.st_size, before.st_mtime_ns)
                # Two complete reads on the same shared handle detect in-place writes
                # even when size and timestamps are restored by the writer.
                stream.seek(0)
                first = stream.read(self.MAX_FILE_BYTES + 1)
                stream.seek(0)
                second = stream.read(self.MAX_FILE_BYTES + 1)
                after = os.fstat(stream.fileno())
                current = self.path.stat()
                if (len(first) != before.st_size or first != second or
                    ((after.st_dev, after.st_ino), after.st_size, after.st_mtime_ns) != signature or
                    ((current.st_dev, current.st_ino), current.st_size, current.st_mtime_ns) != signature):
                    return CsvSnapshot(self.latest, context, None, False, "CSV 기록 중 · 완전한 행을 기다립니다.")
                reset = self.identity is not None and (self.identity != identity or before.st_size < self.offset or
                    self.prefix_hash != hashlib.sha256(first[:self.offset]).digest())
                if reset:
                    self.generation += 1
                    self._clear_file()
                initial = self.header is None
                raw = first[self.offset:self.offset + (self.MAX_FILE_BYTES if initial else 1024 * 1024)]
            self.identity = identity
            combined = self.pending + raw
            quoted = False
            start = 0
            latest = self.latest
            for end, byte in enumerate(combined):
                if byte == 34: quoted = not quoted
                if end - start > self.MAX_ROW_BYTES: raise CsvSourceError("Monitoring CSV 한 행이 64 KiB 상한을 초과했습니다.")
                if byte != 10 or quoted: continue
                encoded = combined[start:end+1]
                start = end + 1
                decoded = encoded.decode("utf-8-sig" if self.header is None else "utf-8")
                rows = list(csv.reader(io.StringIO(decoded), strict=True))
                if len(rows) != 1: raise CsvSourceError("Monitoring CSV 행 구분이 올바르지 않습니다.")
                row = rows[0]
                if self.header is None:
                    if len(row) != len(set(row)) or not set(self.REQUIRED).issubset(row):
                        raise CsvSourceError("Monitoring CSV에 정확한 T_recoating/층/시간 헤더가 없습니다.")
                    self.header = row
                else:
                    sample = self._sample(row, context)
                    if sample:
                        if latest and sample.layer_index < latest.layer_index:
                            raise CsvSourceError("Monitoring CSV 층 순서가 역행했습니다.")
                        if latest and sample.layer_index == latest.layer_index and sample.key != latest.key:
                            raise CsvSourceError("같은 CSV 층의 온도 기록이 변경되어 제어를 보류합니다.")
                        latest = sample
            changed = latest is not None and (self.latest is None or latest.key != self.latest.key)
            self.pending = combined[start:]
            self.offset += len(raw)
            self.latest, self.last_signature = latest, signature
            self.prefix_hash = hashlib.sha256(first[:self.offset]).digest()
            token = None
            waiting_for_anchor = self._history_prefix_open and self._skipped_history_rows > 0
            if not self.pending and self.offset == before.st_size and not waiting_for_anchor:
                token = {"path":str(self.path), "size":before.st_size,
                         "mtime_ticks":before.st_mtime_ns // 100 + 621355968000000000,
                         "sha256":hashlib.sha256(first).hexdigest()}
            fresh = latest is not None and -2 <= self.clock() - latest.finished.timestamp() <= self.MAX_NEW_AGE_SECONDS
            after_connection = latest is not None and latest.finished.timestamp() >= self.connected_at
            is_new = bool(not reset and not self._resume_baseline_pending and changed and (not initial or after_connection)
                          and token is not None and fresh)
            if latest is not None and token is not None:
                self._resume_baseline_pending = False
            details = "CSV 저장된 완료 층을 읽습니다. 측정부터 층 인쇄 완료까지의 지연이 있습니다."
            if waiting_for_anchor:
                waiting = ("재시작 시작 행 확인 · 첫 유효 완료 온도를 기다립니다."
                           if self._resume_anchor_start is not None else "현재 작업 시작 행을 기다립니다.")
                details = f"이전 실행 완료 기록 {self._skipped_history_rows}개 제외 · {waiting}"
            elif self._skipped_history_rows:
                details += f" 이전 실행 완료 기록 {self._skipped_history_rows}개 제외 · 재시작 기준 행 확인."
            return CsvSnapshot(latest, context, token, is_new, details)
        except (OSError, UnicodeError, csv.Error) as exc:
            raise CsvSourceError("Monitoring CSV를 안정적으로 읽을 수 없습니다.") from exc
