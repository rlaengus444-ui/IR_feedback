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
