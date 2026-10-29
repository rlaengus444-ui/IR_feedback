"""Launch the packaged EXE and close only its own Tk window."""
import ctypes
from ctypes import wintypes
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

class ProcessEntry(ctypes.Structure):
    _fields_ = [('dwSize', wintypes.DWORD), ('cntUsage', wintypes.DWORD),
                ('th32ProcessID', wintypes.DWORD), ('th32DefaultHeapID', ctypes.c_size_t),
                ('th32ModuleID', wintypes.DWORD), ('cntThreads', wintypes.DWORD),
                ('th32ParentProcessID', wintypes.DWORD), ('pcPriClassBase', wintypes.LONG),
                ('dwFlags', wintypes.DWORD), ('szExeFile', wintypes.WCHAR * 260)]

kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
kernel.Process32FirstW.restype = wintypes.BOOL
kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
kernel.Process32NextW.restype = wintypes.BOOL
kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL


def direct_children(parent):
    snapshot = kernel.CreateToolhelp32Snapshot(2, 0)  # TH32CS_SNAPPROCESS
    if snapshot == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    result = []
    try:
        entry = ProcessEntry()
        entry.dwSize = ctypes.sizeof(entry)
        more = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            if entry.th32ParentProcessID == parent:
                result.append(entry.th32ProcessID)
            more = kernel.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(snapshot)
    return result


def image_path(handle):
    buffer = ctypes.create_unicode_buffer(32768)
    length = wintypes.DWORD(len(buffer))
    if not kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(length)):
        return None
    return Path(buffer.value).resolve()

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
        # Toolhelp avoids slow/flaky CIM+PowerShell startup. Match both parent
        # PID and full packaged image path before retaining any process handle.
        for pid in direct_children(launcher.pid):
            if pid in child_ids:
                continue
            handle = kernel.OpenProcess(0x101001, False, pid)  # query, synchronize, terminate
            if handle:
                if image_path(handle) == (root / 'runtime/pythonw.exe').resolve():
                    handles[pid] = handle
                    child_ids.add(pid)
                else:
                    kernel.CloseHandle(handle)
        owned = windows({pid for pid, handle in handles.items() if kernel.WaitForSingleObject(handle, 0) == 0x102} | ({launcher.pid} if launcher.poll() is None else set()))
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
    forced = False
    for hwnd, _, _ in windows({pid for pid, handle in handles.items() if kernel.WaitForSingleObject(handle, 0) == 0x102} | ({launcher.pid} if launcher.poll() is None else set())):
        user.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
    for handle in handles.values():
        if kernel.WaitForSingleObject(handle, 5000) == 0x102:
            forced = True
            kernel.TerminateProcess(handle, 1)
            kernel.WaitForSingleObject(handle, 5000)
        kernel.CloseHandle(handle)
    if launcher.poll() is None:
        try:
            launcher.wait(timeout=5)
        except subprocess.TimeoutExpired:
            forced = True
            launcher.terminate()
    if forced:
        raise RuntimeError('Owned launcher/application did not close normally; forced cleanup was required')
    print('Owned application closed normally after WM_CLOSE')
