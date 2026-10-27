"""Validate files required by the standalone application and their source bytes."""
import hashlib
from pathlib import Path
import sys
import zipfile

REQUIRED_APP = (
    'adapter_contracts.py', 'csv_source.py', 'excel_workbook.py', 'excel_worker.ps1',
    'layer_algorithm.py', 'layer_charts.py', 'native_helper.ps1', 'README.ko.md',
    'sequence_field_audit.py', 'sequence_persistence.py', 'session.py', 'single_instance.py',
    'standalone_ir.py', 'target_temperature.py', 'UiaBridge.dll', 'windows_ui_adapter.py',
    'workbook-replay.settings.json',
)
REQUIRED_RUNTIME = ('IRFeedback.exe', 'README.md', 'VALIDATION.ko.md',
                    'runtime/python.exe', 'runtime/pythonw.exe', 'runtime/python314.dll',
                    'runtime/DLLs/_tkinter.pyd', 'runtime/DLLs/tcl86t.dll', 'runtime/DLLs/tk86t.dll')

def normalized(data):
    return data.replace(b'\r\n', b'\n')

def main(path):
    root = Path(__file__).resolve().parent.parent
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        assert len(names) == len(set(names)), 'duplicate ZIP paths'
        assert archive.testzip() is None, 'ZIP CRC error'
        for name in REQUIRED_RUNTIME:
            assert name in names, f'missing runtime/document {name}'
        assert any(name.startswith('runtime/tcl/tcl8.6/') for name in names), 'missing Tcl library'
        assert any(name.startswith('runtime/tcl/tk8.6/') for name in names), 'missing Tk library'
        for name in REQUIRED_APP:
            entry = 'app/' + name
            assert entry in names, f'missing application file {entry}'
            expected = (root / entry).read_bytes()
            actual = archive.read(entry)
            if name.endswith(('.py', '.json', '.md', '.ps1')):
                assert normalized(actual) == normalized(expected), f'different application file {entry}'
            else:
                assert actual == expected, f'different application file {entry}'
        assert normalized(archive.read('VALIDATION.ko.md')) == normalized((root / 'docs/VALIDATION.ko.md').read_bytes())
    print('Package verified:', path, 'sha256:', hashlib.file_digest(Path(path).open('rb'), 'sha256').hexdigest())

if __name__ == '__main__':
    main(sys.argv[1])
