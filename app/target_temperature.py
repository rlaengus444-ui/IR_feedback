"""Validation for the user-selected target used by a feedback session.

The physical sensor and the existing CSV reader use degrees Celsius.  These
bounds are the numeric range accepted by the CSV reader, not a claim about a
particular machine or material's allowed operating temperature.
"""

import math
import threading


MIN_TARGET_C = -273.15
MAX_TARGET_C = 2000.0


def validate_target_temperature(value):
    """Return a numeric target or reject an invalid session configuration."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError("목표 온도는 유효한 숫자여야 합니다.")
    try:
        is_finite = math.isfinite(value)
    except OverflowError:
        is_finite = False
    if not is_finite:
        raise ValueError("목표 온도는 유효한 숫자여야 합니다.")
    if not MIN_TARGET_C <= value <= MAX_TARGET_C:
        raise ValueError("목표 온도는 -273.15~2000°C 범위로 입력하세요.")
    return float(value)


def parse_target_temperature(value):
    """Parse an entry-field value with the same checks as the session API."""
    if not isinstance(value, str):
        raise ValueError("목표 온도는 유효한 숫자여야 합니다.")
    try:
        parsed = float(value.strip())
    except (ValueError, OverflowError):
        raise ValueError("목표 온도는 유효한 숫자여야 합니다.") from None
    return validate_target_temperature(parsed)


class TargetTemperatureSetting:
    """A session-owned, thread-safe target requested by the operator."""

    def __init__(self, value):
        self._lock = threading.Lock()
        self._value = validate_target_temperature(value)

    def set(self, value):
        validated = validate_target_temperature(value)
        with self._lock:
            self._value = validated
        return validated

    def get(self):
        with self._lock:
            return self._value
