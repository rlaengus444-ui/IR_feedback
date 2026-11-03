"""Connection-fixed IR bound and a small, versioned local preference file."""
import json
import math
import os
from pathlib import Path
import tempfile


def validate_maximum_ir(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError("최대 IR 출력은 0~100%의 숫자로 입력하세요.")
    try:
        valid = math.isfinite(value) and 0 <= value <= 100
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError("최대 IR 출력은 0~100%의 숫자로 입력하세요.")
    return float(value)


def parse_maximum_ir(value):
    if not isinstance(value, str):
        raise ValueError("최대 IR 출력은 0~100%의 숫자로 입력하세요.")
    try:
        parsed = float(value.strip())
    except (ValueError, OverflowError):
        raise ValueError("최대 IR 출력은 0~100%의 숫자로 입력하세요.") from None
    return validate_maximum_ir(parsed)


class MaximumIrPreference:
    """Only this new option is stored; algorithm JSON files are never modified.

    Missing file means the compatible default 100. Invalid/unreadable files
    raise, so the GUI requires an explicit entry instead of widening the cap.
    """
    def __init__(self, path=None):
        base = Path(os.environ.get('LOCALAPPDATA') or Path.home() / '.local/share')
        self.path = Path(path) if path is not None else base / 'IRFeedback/ir-output-limit.json'

    def load(self):
        try:
            with self.path.open('rb') as source:
                data = source.read(4097)
        except FileNotFoundError:
            return 100.0
        if len(data) > 4096:
            raise ValueError('최대 IR 출력 설정 파일이 너무 큽니다.')
        try:
            value = json.loads(data.decode('utf-8'))
            if (not isinstance(value, dict) or set(value) != {'version', 'maximum_ir_percent'}
                    or type(value['version']) is not int or value['version'] != 1):
                raise ValueError('지원하지 않는 최대 IR 출력 설정입니다.')
            return validate_maximum_ir(value['maximum_ir_percent'])
        except (UnicodeError, json.JSONDecodeError) as error:
            raise ValueError('최대 IR 출력 설정 파일을 읽지 못했습니다.') from error

    def save(self, value):
        value = validate_maximum_ir(value)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        name = None
        try:
            with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=self.path.parent,
                    prefix='.ir-output-limit-', suffix='.tmp', delete=False) as output:
                name = output.name
                json.dump({'version': 1, 'maximum_ir_percent': value}, output)
                output.write('\n')
                output.flush()
                os.fsync(output.fileno())
            os.replace(name, self.path)
        finally:
            if name is not None:
                Path(name).unlink(missing_ok=True)


try:
    from .adapter_contracts import ObservationSuspended
except ImportError:
    from adapter_contracts import ObservationSuspended
import threading


class LiveMaximumIrChanged(ObservationSuspended):
    """A limit decision is no longer authorized for admission."""


def limit_ir_recommendation(actual, raw, cap, minimum=0, hard_maximum=100, step=5):
    raw = validate_maximum_ir(raw)
    minimum = validate_maximum_ir(minimum)
    hard_maximum = validate_maximum_ir(hard_maximum)
    cap = validate_maximum_ir(cap)
    actual = validate_maximum_ir(actual)
    if minimum > hard_maximum or not minimum <= actual <= hard_maximum or cap < minimum:
        raise ValueError('IR 출력 또는 상한이 허용 범위를 벗어났습니다.')
    try:
        valid_step = isinstance(step, (int, float)) and not isinstance(step, bool) and math.isfinite(step) and 0 < step <= 5
    except OverflowError:
        valid_step = False
    if not valid_step:
        raise ValueError('IR 출력 변경 폭은 0보다 크고 5 이하여야 합니다.')
    effective = min(cap, hard_maximum)
    if actual > effective:
        return max(effective, actual - step)
    return min(effective, actual + step, max(minimum, actual - step, raw))


class LiveMaximumIrSetting:
    """Versioned cap and short, non-I/O admission critical sections."""
    def __init__(self, value):
        self._lock = threading.Lock()
        self._value = validate_maximum_ir(value)
        self._serial = 0
        self._active = None
        self._bounds = None
        self._in_flight = None

    def bind_bounds(self, minimum, hard_maximum, step=5):
        minimum = validate_maximum_ir(minimum)
        hard_maximum = validate_maximum_ir(hard_maximum)
        if minimum > hard_maximum:
            raise ValueError('IR 출력 하한이 상한보다 높습니다.')
        limit_ir_recommendation(minimum, minimum, minimum, minimum, hard_maximum, step)
        bounds = (minimum, hard_maximum, float(step))
        with self._lock:
            if self._bounds is not None and self._bounds != bounds:
                raise ValueError('연결된 IR 출력 범위는 변경할 수 없습니다.')
            if self._value < minimum:
                raise ValueError('최대 IR 출력은 알고리즘 하한보다 작을 수 없습니다.')
            self._bounds = bounds

    def snapshot(self):
        with self._lock:
            return self._serial, self._value

    def set(self, value):
        value = validate_maximum_ir(value)
        with self._lock:
            if self._bounds is None:
                raise ValueError('IR 출력 범위가 아직 준비되지 않았습니다.')
            if value < self._bounds[0]:
                raise ValueError('최대 IR 출력은 알고리즘 하한보다 작을 수 없습니다.')
            if value != self._value:
                self._value = value
                self._serial += 1
            return value

    def _matches(self, stamp):
        return (type(stamp) is tuple and len(stamp) == 2 and type(stamp[0]) is int
                and type(stamp[1]) is float and stamp == (self._serial, self._value))

    def activate(self, stamp):
        with self._lock:
            if self._bounds is None or not self._matches(stamp):
                return False
            self._active = (stamp, min(self._value, self._bounds[1]))
            return True

    def status(self):
        with self._lock:
            minimum, hard, step = self._bounds if self._bounds else (None, None, None)
            return dict(requested_stamp=(self._serial, self._value), requested=self._value,
                        effective_cap=min(self._value, hard) if hard is not None else None,
                        active_stamp=self._active[0] if self._active else None,
                        active_effective_cap=self._active[1] if self._active else None,
                        pending=self._active is None or self._active[0] != (self._serial, self._value),
                        in_flight=self._in_flight is not None, ready=self._bounds is not None,
                        minimum=minimum, hard_maximum=hard, step=step)

    def begin_admission(self, stamp, proposed, actual):
        with self._lock:
            if (self._bounds is None or self._active is None or self._in_flight is not None or
                    not self._matches(stamp) or self._active[0] != stamp):
                raise LiveMaximumIrChanged('IR 상한이 변경되어 새 관찰을 기다립니다.')
            minimum, hard, step = self._bounds
            try:
                permitted = limit_ir_recommendation(actual, proposed, self._value, minimum, hard, step)
                proposed = validate_maximum_ir(proposed)
            except ValueError as exc:
                raise LiveMaximumIrChanged('IR 출력이 현재 상한 또는 변경 폭과 맞지 않습니다.') from exc
            if abs(permitted - proposed) > 1e-9:
                raise LiveMaximumIrChanged('IR 출력이 현재 상한 또는 변경 폭과 맞지 않습니다.')
            token = object()
            self._in_flight = token
            return token

    def end_admission(self, token):
        with self._lock:
            if token is not self._in_flight or token is None:
                raise ValueError('IR 출력 승인 토큰이 일치하지 않습니다.')
            self._in_flight = None
