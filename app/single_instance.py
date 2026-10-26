"""One module instance per Windows logon session; no target-process interaction."""

import ctypes
from ctypes import wintypes
import os


class SingleInstance:
    NAME = r"Local\MADDE.IRFeedback.Standalone"

    def __init__(self, name=None):
        self.name = name or self.NAME
        self.handle = None
        self.kernel = None

    def acquire(self):
        if os.name != "nt":
            raise OSError("이 외부 모듈은 Windows에서 실행하세요.")
        if self.handle is not None:
            return True
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        kernel.CreateMutexW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        ctypes.set_last_error(0)
        handle = kernel.CreateMutexW(None, False, self.name)
        error = ctypes.get_last_error()
        if not handle:
            raise ctypes.WinError(error)
        if error == 183:  # ERROR_ALREADY_EXISTS
            kernel.CloseHandle(handle)
            return False
        self.kernel = kernel
        self.handle = handle
        return True

    def close(self):
        if self.handle is not None:
            self.kernel.CloseHandle(self.handle)
            self.handle = None
