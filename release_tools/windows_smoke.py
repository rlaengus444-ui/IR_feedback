"""Launch the packaged EXE and close only its own Tk window."""
import ctypes
from ctypes import wintypes
import json
from pathlib import Path
import subprocess
import sys
import time

root = Path(sys.argv[1]).resolve()
user = ctypes.WinDLL('user32', use_last_error=True)
user.EnumWindows.argtypes = [ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM), wintypes.LPARAM]
user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
callback_type = user.EnumWindows.argtypes[0]
kernel = ctypes.WinDLL('kernel32', use_last_error=True)
kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel.OpenProcess.restype = wintypes.HANDLE
kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
kernel.CloseHandle.argtypes = [wintypes.HANDLE]

def windows(pids):
    found = []
    @callback_type
    def callback(hwnd, _):
        pid = wintypes.DWORD()
        user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids:
            buf = ctypes.create_unicode_buffer(user.GetWindowTextLengthW(hwnd) + 1)
            user.GetWindowTextW(hwnd, buf, len(buf))
            if buf.value:
                found.append((hwnd, pid.value, buf.value))
        return True
    user.EnumWindows(callback, 0)
    return found

launcher = subprocess.Popen([str(root / 'IRFeedback.exe')], cwd=root)
child_ids = set()
handles = {}
try:
    deadline = time.monotonic() + 35
    target = None
    while time.monotonic() < deadline:
        command = (f"Get-CimInstance Win32_Process -Filter 'ParentProcessId={launcher.pid}' | "
                   "Select-Object ProcessId,ExecutablePath | ConvertTo-Json -Compress")
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', command], capture_output=True, text=True, timeout=10, check=True)
        if result.stdout.strip():
            rows = json.loads(result.stdout)
            for row in rows if isinstance(rows, list) else [rows]:
                if row.get('ExecutablePath') and Path(row['ExecutablePath']).resolve() == (root / 'runtime/pythonw.exe').resolve():
                    pid = row['ProcessId']
                    if pid not in child_ids:
                        handle = kernel.OpenProcess(0x100001, False, pid)  # synchronize and terminate
                        if handle:
                            handles[pid] = handle
                            child_ids.add(pid)
        owned = windows(child_ids | {launcher.pid})
        errors = [title for _, _, title in owned if '오류' in title]
        if errors:
            raise RuntimeError('Launcher/application error window: ' + repr(errors))
        target = next(((hwnd, pid) for hwnd, pid, title in owned if pid in child_ids and title == 'IR 피드백 외부 모듈 · CSV'), None)
        if target:
            break
        time.sleep(.2)
    if not target:
        raise RuntimeError(f'Packaged launcher window not found; child PIDs={child_ids}')
    if launcher.wait(timeout=5) != 0:
        raise RuntimeError('Launcher exited with an error')
    print(f'Observed launcher PID {launcher.pid}, packaged pythonw PID {target[1]} and expected window')
finally:
    for hwnd, _, _ in windows(child_ids | {launcher.pid}):
        user.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
    for handle in handles.values():
        if kernel.WaitForSingleObject(handle, 5000) == 0x102:
            kernel.TerminateProcess(handle, 1)
            kernel.WaitForSingleObject(handle, 5000)
        kernel.CloseHandle(handle)
    if launcher.poll() is None:
        try:
            launcher.wait(timeout=5)
        except subprocess.TimeoutExpired:
            launcher.terminate()
