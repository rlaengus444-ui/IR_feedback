"""Read an explicitly user-bound Home path leaf; never enumerate a UI tree."""
from dataclasses import dataclass
import ntpath
import os
import re
import time

from adapter_contracts import MachineWindow
from slice_data import EXTENSIONS


class SourceReadError(RuntimeError):
    pass


class BindingLost(SourceReadError):
    pass


def canonical(path):
    return ntpath.normcase(ntpath.normpath(str(path)))


def parse_path_text(text):
    """Same strict grammar as native_helper's SourceContainer; no shell expansion."""
    text = str(text).strip()
    if not text or len(text) > 4095 or any(c in text for c in ('\0', '\r', '\n', '…')):
        raise SourceReadError('경로가 비어 있거나 전체 경로를 읽을 수 없습니다.')
    matches = list(re.finditer(r'[A-Za-z]:[\\/]|\\\\', text))
    if len(matches) != 1:
        raise SourceReadError('하나의 절대 이미지 폴더 또는 이미지 파일 경로가 필요합니다.')
    start = matches[0].start()
    prefix = text[:start].strip().strip('"\'').strip().rstrip(':=').strip().casefold()
    allowed = ('', 'path', 'folder', 'image path', 'image folder', 'slicing path',
               'slicing image path', '경로', '폴더', '이미지 경로', '이미지 폴더',
               '슬라이싱 경로', '슬라이싱 이미지 경로')
    if prefix not in allowed:
        raise SourceReadError('경로 표시 형식을 확인할 수 없습니다. 경로 텍스트 칸을 직접 지정하세요.')
    path = text[start:].strip().rstrip('"\'').strip()
    if any(c in path for c in ('*', '?', '<', '>', '|', '"')) or not ntpath.isabs(path):
        raise SourceReadError('잘못된 경로 또는 생략된 경로입니다.')
    if path.startswith(('\\\\.\\', '\\\\?\\')) or '...' in path:
        raise SourceReadError('장치 경로나 생략된 경로는 자동으로 사용하지 않습니다.')
    suffix = ntpath.splitext(path)[1].lower()
    if suffix == '.zip':
        raise SourceReadError('자동 입력은 이미지 폴더/이미지 파일 경로를 사용하세요. ZIP은 수동 불러오기를 사용합니다.')
    folder = ntpath.dirname(path) if suffix in EXTENSIONS else path
    return ntpath.normpath(folder)


@dataclass(frozen=True)
class PathBinding:
    target: MachineWindow
    leaf: int
    home: int
    chain: tuple
    home_caption: str

    def token(self, directory):
        return {'hwnd': self.leaf, 'home': self.home, 'home_caption': self.home_caption,
                'directory': canonical(directory), 'chain': self.chain}


@dataclass(frozen=True)
class SourceSelection:
    directory: str
    raw_text: str
    binding: PathBinding


class WindowsPathReader:
    """No import-time Windows access; no UIA, Process table, click or edit API."""
    HOMES = ('home', '홈')

    def __init__(self):
        self._api = None

    def _load(self):
        if self._api is not None:
            return
        if os.name != 'nt':
            raise SourceReadError('첫 탭 경로 읽기는 Windows에서 사용할 수 있습니다.')
        import ctypes as c
        from ctypes import wintypes as w
        api = c.WinDLL('user32', use_last_error=True)
        signatures = {
            'IsWindow': ([w.HWND], w.BOOL),
            'GetParent': ([w.HWND], w.HWND),
            'GetWindowThreadProcessId': ([w.HWND, c.POINTER(w.DWORD)], w.DWORD),
            'GetDlgCtrlID': ([w.HWND], c.c_int),
            'GetClassNameW': ([w.HWND, w.LPWSTR, c.c_int], c.c_int),
            'GetWindowTextW': ([w.HWND, w.LPWSTR, c.c_int], c.c_int),
            'GetCursorPos': ([c.POINTER(w.POINT)], w.BOOL),
            'WindowFromPoint': ([w.POINT], w.HWND),
            'ScreenToClient': ([w.HWND, c.POINTER(w.POINT)], w.BOOL),
            'ChildWindowFromPointEx': ([w.HWND, w.POINT, w.UINT], w.HWND),
            'IsChild': ([w.HWND, w.HWND], w.BOOL),
            'SendMessageTimeoutW': ([w.HWND, w.UINT, c.c_size_t, c.c_void_p,
                                    w.UINT, w.UINT, c.POINTER(c.c_size_t)], c.c_ssize_t),
        }
        for name, (args, result) in signatures.items():
            fn = getattr(api, name); fn.argtypes = args; fn.restype = result
        self._api, self._c, self._w = api, c, w

    def _owned(self, target, handle):
        api, c, w = self._api, self._c, self._w
        pid = w.DWORD()
        if (not api.IsWindow(target.hwnd) or not api.IsWindow(handle)
                or not api.GetWindowThreadProcessId(handle, c.byref(pid))
                or pid.value != target.pid):
            raise BindingLost('지정한 경로 표시 칸이 없어졌습니다. 다시 지정하세요.')
        root_pid = w.DWORD()
        api.GetWindowThreadProcessId(target.hwnd, c.byref(root_pid))
        if root_pid.value != target.pid or (handle != target.hwnd and not api.IsChild(target.hwnd, handle)):
            raise BindingLost('선택한 BJT 창에 속한 경로 칸이 아닙니다.')

    def _class(self, handle):
        value = self._c.create_unicode_buffer(256)
        if not self._api.GetClassNameW(handle, value, len(value)):
            raise BindingLost('경로 표시 칸의 종류를 확인할 수 없습니다.')
        return value.value

    def _text(self, target, handle):
        self._owned(target, handle)
        value = self._c.create_unicode_buffer(4097); count = self._c.c_size_t()
        if not self._api.SendMessageTimeoutW(handle, 0x000D, len(value),
                self._c.cast(value, self._c.c_void_p), 0x23, 250, self._c.byref(count)):
            raise SourceReadError('경로 표시 응답 대기 중입니다.')
        self._owned(target, handle)
        if count.value >= 4096:
            raise SourceReadError('경로 표시가 허용 길이를 초과했습니다.')
        return value.value

    def _caption(self, target, handle):
        # Cross-process GetWindowText reads the native caption cache. Unlike
        # WM_GETTEXT it does not call an unknown ancestor/grid's WndProc.
        self._owned(target,handle)
        value=self._c.create_unicode_buffer(4097)
        count=self._api.GetWindowTextW(handle,value,len(value))
        self._owned(target,handle)
        if count>=4096:raise SourceReadError('첫 탭 이름의 길이를 확인하세요.')
        return value.value

    def cursor(self):
        self._load(); point = self._w.POINT()
        if not self._api.GetCursorPos(self._c.byref(point)):
            raise SourceReadError('마우스 위치를 읽을 수 없습니다.')
        return point.x, point.y

    def bind_at(self, target, x, y):
        self._load(); api = self._api
        screen = self._w.POINT(x, y)
        hit = api.WindowFromPoint(screen)
        self._owned(target, hit)
        # WindowFromPoint skips STATIC text. Descend only the pointed native
        # child chain, including STATIC, without enumerating sibling controls.
        handle = target.hwnd
        for _ in range(64):
            point = self._w.POINT(x, y)
            if not api.ScreenToClient(handle, self._c.byref(point)):
                raise BindingLost('지정 위치를 확인할 수 없습니다.')
            child = api.ChildWindowFromPointEx(handle, point, 1)
            if not child or child == handle:
                break
            self._owned(target, child)
            if any(s in self._class(child).upper() for s in ('GRID', 'TABLE', 'LISTVIEW', 'LISTBOX')):
                raise SourceReadError('표 대신 첫 탭의 경로 텍스트 칸을 지정하세요.')
            handle = child
        else:
            raise SourceReadError('경로 표시 칸의 깊이가 허용 범위를 초과했습니다.')
        return self.bind_handle(target, handle)

    def bind_handle(self, target, handle):
        """Explicit leaf binding. bind_at is the production user-pointed entry."""
        self._load(); self._owned(target, handle)
        kind = self._class(handle).upper()
        if not any(s in kind for s in ('STATIC', 'EDIT')):
            raise SourceReadError('읽을 수 있는 경로 텍스트 칸을 지정하세요.')
        chain = []; home = None; caption = None; current = handle
        deadline = time.monotonic() + 2
        for _ in range(64):
            if time.monotonic() > deadline:
                raise SourceReadError('경로 칸 확인 시간이 초과했습니다.')
            self._owned(target, current)
            cls = self._class(current); parent = int(self._api.GetParent(current) or 0)
            chain.append((int(current), cls, int(self._api.GetDlgCtrlID(current)), parent))
            if any(s in cls.upper() for s in ('GRID', 'TABLE', 'LISTVIEW', 'LISTBOX')):
                raise SourceReadError('표 안의 경로는 자동 입력에 사용하지 않습니다.')
            if current == target.hwnd:
                break
            if current != handle and 'WINDOW' in cls.upper():
                text = self._caption(target, current).strip()
                if text.casefold() == 'process':
                    raise SourceReadError('Process 화면은 경로 입력으로 사용하지 않습니다.')
                if text.casefold() in self.HOMES:
                    if home is not None:
                        raise SourceReadError('첫 탭의 소속이 모호합니다.')
                    home, caption = int(current), text
            current = parent
        else:
            raise SourceReadError('경로 칸의 부모 구조를 확인할 수 없습니다.')
        if home is None:
            raise SourceReadError('BJT 첫 탭(Home)의 경로 표시 칸을 지정하세요.')
        binding = PathBinding(target, int(handle), home, tuple(chain), caption)
        return binding, self.read(binding)

    def read(self, binding):
        self._load(); target = binding.target
        for handle, cls, control_id, parent in binding.chain:
            self._owned(target, handle)
            if (self._class(handle) != cls or self._api.GetDlgCtrlID(handle) != control_id
                    or int(self._api.GetParent(handle) or 0) != parent):
                raise BindingLost('경로 표시 칸이 다시 생성되거나 바뀌었습니다. 다시 지정하세요.')
        if self._caption(target, binding.home).strip() != binding.home_caption:
            raise BindingLost('지정한 첫 탭이 바뀌었습니다. 경로 칸을 다시 지정하세요.')
        raw = self._text(target, binding.leaf)
        return SourceSelection(parse_path_text(raw), raw, binding)
