"""Read-only evidence for the original Sequences IR field + Save operation.

No method writes a file or changes the original application. A matching saved
parameter proves persistence of the requested setting, not measured lamp power.
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import math
import os
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Mapping

MAXIMUM_OWNER_FILE_BYTES = 256 * 1024
MAXIMUM_OWNER_FILES = 64
MAXIMUM_CONFIG_BYTES = 4 * 1024 * 1024
IR_SCOPE = "Printing"
IR_NAME = "IRHeatingOutputPct"


class PersistenceUnavailable(RuntimeError):
    pass


class PersistenceMismatch(PersistenceUnavailable):
    pass


@dataclass(frozen=True)
class SequencePersistenceSnapshot:
    path: str
    power: float
    sha256: str
    mtime_ns: int
    non_ir: Mapping[str, object]

    @property
    def file_path(self) -> str:
        return self.path


def _read_shared(path: Path) -> bytes:
    """Allow the application's normal atomic File.Replace while reading on Windows."""
    if os.name != "nt":
        with path.open("rb") as stream:
            return stream.read(MAXIMUM_OWNER_FILE_BYTES + 1)
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                               ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    kernel.ReadFile.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateFileW(str(path), 0x80000000, 0x1 | 0x2 | 0x4,
                                None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        buffer = ctypes.create_string_buffer(MAXIMUM_OWNER_FILE_BYTES + 1)
        count = wintypes.DWORD()
        if not kernel.ReadFile(handle, buffer, len(buffer), ctypes.byref(count), None):
            raise ctypes.WinError(ctypes.get_last_error())
        return buffer.raw[:count.value]
    finally:
        kernel.CloseHandle(handle)


def _identity(stat) -> tuple:
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns


def _read_entries(path: Path):
    try:
        before = path.stat()
        if before.st_size > MAXIMUM_OWNER_FILE_BYTES:
            raise PersistenceUnavailable("An owner parameter file exceeds the 256 KiB read bound.")
        raw = _read_shared(path)
        after = path.stat()
        if len(raw) > MAXIMUM_OWNER_FILE_BYTES or _identity(before) != _identity(after):
            raise PersistenceUnavailable("An owner parameter file changed during the bounded read.")
        entries = json.loads(raw.decode("utf-8-sig"))
    except (OSError, ValueError, UnicodeError) as exc:
        raise PersistenceUnavailable("The saved owner parameters could not be read reliably.") from exc
    if not isinstance(entries, list) or len(entries) > 4096:
        raise PersistenceUnavailable("Owner parameters must contain a bounded JSON array.")
    values: dict[str, object] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise PersistenceUnavailable("A BJT parameter entry is invalid.")
        scope, name = entry.get("Scope"), entry.get("Name")
        if not isinstance(scope, str) or not isinstance(name, str) or not scope or not name:
            raise PersistenceUnavailable("A BJT parameter has no exact Scope/Name identity.")
        key = scope + "\x1f" + name
        if key in values:
            raise PersistenceUnavailable("Duplicate saved parameter identities are ambiguous.")
        values[key] = (entry.get("Type"), entry.get("Value"))
    return entries, values, raw, after


def _inventory(directory: Path):
    files = {}
    total = 0
    root = directory.resolve()
    for path in sorted(directory.glob("*.parameters.json"), key=lambda item: item.name.casefold()):
        if not path.is_file():
            continue
        if path.is_symlink() or path.resolve().parent != root:
            raise PersistenceUnavailable("Owner parameter paths must remain in the selected Configs directory.")
        key = path.name.casefold()
        if key in files:
            raise PersistenceUnavailable("Owner parameter paths are ambiguous.")
        stat = path.stat()
        total += stat.st_size
        files[key] = (path, _identity(stat))
        if len(files) > MAXIMUM_OWNER_FILES or total > MAXIMUM_CONFIG_BYTES:
            raise PersistenceUnavailable("Owner parameter verification exceeds 64 files or 4 MiB.")
    return files


def _read(path: Path) -> SequencePersistenceSnapshot:
    try:
        inventory = _inventory(path.parent)
        values: dict[str, object] = {}
        power = None
        ir_raw = None
        ir_stat = None
        for file_name, (owner_path, _) in inventory.items():
            entries, parameters, raw, stat = _read_entries(owner_path)
            values[file_name + "\x1f__file__"] = True
            for identity, value in parameters.items():
                if owner_path == path and identity == IR_SCOPE + "\x1f" + IR_NAME:
                    continue
                values[file_name + "\x1f" + identity] = value
            if owner_path != path:
                continue
            ir_raw, ir_stat = raw, stat
            power = _ir_value(entries)
        after_inventory = _inventory(path.parent)
        if inventory != after_inventory:
            raise PersistenceUnavailable("Owner parameter files changed during the aggregate read.")
    except OSError as exc:
        raise PersistenceUnavailable("Owner parameter files could not be inspected reliably.") from exc
    if ir_raw is None or power is None:
        raise PersistenceUnavailable("The saved BJT parameter file disappeared during verification.")
    return SequencePersistenceSnapshot(str(path), power, hashlib.sha256(ir_raw).hexdigest(),
                                       ir_stat.st_mtime_ns, values)


def _ir_value(entries) -> float:
    power = None
    for entry in entries:
        scope, name = entry.get("Scope"), entry.get("Name")
        if scope == IR_SCOPE and name == IR_NAME:
            value = entry.get("Value")
            if isinstance(value, bool) or not isinstance(value, (str, int, float)):
                raise PersistenceUnavailable("The saved IR setting is not numeric.")
            try:
                number = Decimal(str(value))
                if not number.is_finite() or not Decimal(0) <= number <= Decimal(100):
                    raise PersistenceUnavailable("The saved IR setting is outside 0..100 percent.")
                power = float(number)
            except InvalidOperation as exc:
                raise PersistenceUnavailable("The saved IR setting is not a finite decimal.") from exc
    if power is None:
        raise PersistenceUnavailable("No exact Printing/IRHeatingOutputPct entry was saved.")
    return power


def capture(target) -> SequencePersistenceSnapshot | None:
    """Read only known existing config paths derived from the selected process image.

    The canonical Operator layout uses Machines/<machine>/Configs. The exact
    Configs sibling is the source's non-activated runtime layout fallback. When
    both exist their ownership is ambiguous, so neither is selected implicitly.
    """
    machine = str(target.machine).upper()
    image = Path(target.image_path or "")
    if machine not in ("M1", "S3") or not image.is_absolute():
        return None
    directory = image.parent
    candidates = [directory / "Machines" / machine / "Configs" / "BJT.parameters.json",
                  directory / "Configs" / "BJT.parameters.json"]
    existing = [path for path in candidates if path.is_file()]
    if not existing:
        return None
    if len(existing) != 1:
        raise PersistenceUnavailable("Multiple existing BJT config layouts are ambiguous.")
    return _read(existing[0])


def verify_after(before: SequencePersistenceSnapshot | None, power: int | float,
                 timeout: float = 1.0) -> SequencePersistenceSnapshot | None:
    """Wait briefly for one already-invoked Save, without retrying the UI command."""
    if before is None:
        return None
    if isinstance(power, bool) or not isinstance(power, (int, float)) or not math.isfinite(power) or not 0 <= power <= 100:
        raise ValueError("Expected IR output must be finite and within 0..100 percent.")
    if not math.isfinite(timeout) or timeout < 0 or timeout > 2:
        raise ValueError("Persistence verification must have a 0..2 second bound.")
    deadline = time.monotonic() + timeout
    last_error = None
    while True:
        try:
            current = _read(Path(before.path))
            if current.non_ir != before.non_ir:
                raise PersistenceMismatch("Non-IR owner parameters changed during Sequence Save.")
            if math.isclose(current.power, power, rel_tol=0, abs_tol=1e-9):
                return current
        except PersistenceMismatch:
            raise
        except PersistenceUnavailable as exc:
            last_error = exc
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise PersistenceMismatch("Sequence Save did not produce the expected saved IR setting within the verification bound.") from last_error
        time.sleep(min(0.05, remaining))
