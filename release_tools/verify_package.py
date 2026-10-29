"""Verify a slice release ZIP against its source tree and complete hash manifest."""
import hashlib
import json
from pathlib import Path
import re
import sys
import zipfile

ROOT = Path(__file__).resolve().parent.parent
REQUIRED_APP = (
    'adapter_contracts.py', 'csv_source.py', 'excel_workbook.py', 'excel_worker.ps1',
    'layer_algorithm.py', 'layer_charts.py', 'plot_archive.py', 'plot_images.py', 'native_helper.ps1', 'README.ko.md',
    'sequence_field_audit.py', 'sequence_persistence.py', 'session.py', 'single_instance.py',
    'standalone_ir.py', 'target_temperature.py', 'UiaBridge.dll', 'windows_ui_adapter.py',
    'workbook-replay.settings.json', 'slice_data.py', 'slice_panel.py', 'slice_workbook.py',
    'templates/IR_슬라이싱_수동계산.xlsx',
)
SOURCES = {**{'app/' + name: 'app/' + name for name in REQUIRED_APP},
           'README.md': 'README.md', 'VERSION.txt': 'VERSION.txt',
           'VALIDATION.ko.md': 'docs/SLICE_LIVE.ko.md',
           'docs/SLICE_LIVE.ko.md': 'docs/SLICE_LIVE.ko.md',
           'docs/LIVE_SETTINGS.ko.md': 'docs/LIVE_SETTINGS.ko.md',
           'docs/CSV_RESUME_ANCHOR.ko.md': 'docs/CSV_RESUME_ANCHOR.ko.md',
           'docs/validation/csv-resume-anchor-2026.10.29-review.json': 'docs/validation/csv-resume-anchor-2026.10.29-review.json',
           'docs/validation/csv-resume-anchor-2026.10.29-offline.json': 'docs/validation/csv-resume-anchor-2026.10.29-offline.json',
           'docs/validation/csv-resume-real-data-2026.10.29.json': 'docs/validation/csv-resume-real-data-2026.10.29.json',
           'docs/S3_DISCOVERY.ko.md': 'docs/S3_DISCOVERY.ko.md',
           'docs/BRIDGE_VALIDATION.ko.md': 'docs/BRIDGE_VALIDATION.ko.md',
           'SLICE_INPUT.ko.md': 'docs/SLICE_INPUT.ko.md',
           'docs/SLICE_INPUT.ko.md': 'docs/SLICE_INPUT.ko.md',
           'docs/SLICE_VALIDATION.ko.md': 'docs/SLICE_VALIDATION.ko.md',
           'docs/VALIDATION.ko.md': 'docs/VALIDATION.ko.md'}
REQUIRED_RUNTIME = ('IRFeedback.exe', 'runtime/python.exe', 'runtime/pythonw.exe',
                    'runtime/python314.dll', 'runtime/DLLs/_tkinter.pyd',
                    'runtime/DLLs/tcl86t.dll', 'runtime/DLLs/tk86t.dll',
                    'runtime/Lib/site-packages/PIL/__init__.py',
                    'runtime/Lib/site-packages/PIL/Image.py')
MANIFEST = 'MANIFEST.json'

def check(condition, message):
    if not condition:
        raise ValueError(message)

def verify(path, version, root=ROOT):
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        names = [item.filename for item in infos]
        check(len(names) == len(set(names)), 'duplicate ZIP paths')
        folded = set()
        for info in infos:
            name = info.filename
            parts = name.rstrip('/').split('/')
            check(bool(name) and not name.startswith('/') and '\\' not in name and
                  all(p not in ('', '.', '..') and not p.endswith((' ', '.')) and ':' not in p for p in parts),
                  f'unsafe ZIP path: {name}')
            key = name.rstrip('/').casefold()
            check(key not in folded, f'colliding ZIP path: {name}')
            folded.add(key)
            check(not (info.external_attr >> 16 & 0o170000 == 0o120000), f'symlink ZIP entry: {name}')
        check(archive.testzip() is None, 'ZIP CRC error')
        files = {item.filename for item in infos if not item.is_dir()}
        for name in (*REQUIRED_RUNTIME, *SOURCES, MANIFEST):
            check(name in files, f'missing required file {name}')
        check(any(re.fullmatch(r'runtime/Lib/site-packages/PIL/_imaging(?:\.cp314-win_amd64)?\.pyd', n, re.I) for n in files), 'missing Pillow _imaging.pyd')
        check(any(re.fullmatch(r'runtime/Lib/site-packages/pillow-[^/]+\.dist-info/METADATA', n, re.I) for n in files), 'missing Pillow metadata')
        check(any(re.fullmatch(r'runtime/Lib/site-packages/pillow-[^/]+\.dist-info/licenses/LICENSE', n, re.I) for n in files), 'missing Pillow license notices')
        check(archive.read('VERSION.txt').decode('utf-8-sig').strip() == version, 'source version mismatch')
        for prefix in ('runtime/tcl/tcl8.6/', 'runtime/tcl/tk8.6/'):
            check(any(name.startswith(prefix) for name in files), f'missing library {prefix}')
        # Standard full CPython uses Lib/site-packages; a ._pth file overrides it.
        pth = [n for n in files if re.fullmatch(r'runtime/python\d+\._pth', n, re.I)]
        for name in pth:
            paths = [line.strip().replace('\\', '/').lower() for line in archive.read(name).decode('utf-8-sig').splitlines() if line.strip() and not line.lstrip().startswith('#')]
            check('lib/site-packages' in paths, f'Pillow missing from Python load path in {name}')
        for entry, source in SOURCES.items():
            check(archive.read(entry) == (Path(root) / source).read_bytes(), f'different source file {entry}')
        manifest = json.loads(archive.read(MANIFEST))
        check(isinstance(manifest, dict) and manifest.get('version') == version, 'manifest version mismatch')
        hashes = manifest.get('sha256')
        check(isinstance(hashes, dict) and set(hashes) == files - {MANIFEST}, 'manifest file list mismatch')
        for name, expected in hashes.items():
            check(isinstance(expected, str) and re.fullmatch(r'[0-9a-f]{64}', expected) is not None and
                  hashlib.sha256(archive.read(name)).hexdigest() == expected, f'manifest hash mismatch: {name}')
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def main():
    if len(sys.argv) != 3:
        raise SystemExit('Usage: python release_tools/verify_package.py PACKAGE.zip VERSION')
    print('Package verified:', sys.argv[1], 'sha256:', verify(sys.argv[1], sys.argv[2]))

if __name__ == '__main__':
    main()
