import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from verify_package import MANIFEST, REQUIRED_RUNTIME, SOURCES, verify

class PackageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.entries = {name: b'content' for name in REQUIRED_RUNTIME}
        self.entries['runtime/Lib/site-packages/PIL/_imaging.pyd'] = b'pyd'
        self.entries['runtime/Lib/site-packages/pillow-13.0.0.dist-info/METADATA'] = b'Name: pillow'
        self.entries['runtime/Lib/site-packages/pillow-13.0.0.dist-info/licenses/LICENSE'] = b'License notices'
        self.entries.update({'runtime/tcl/tcl8.6/init.tcl': b'tcl', 'runtime/tcl/tk8.6/tk.tcl': b'tk'})
        for entry, source in SOURCES.items():
            file = self.root / source
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(b'test\n' if entry == 'VERSION.txt' else source.encode())
            self.entries[entry] = file.read_bytes()

    def package(self, change=None):
        entries = dict(self.entries)
        if change:
            change(entries)
        manifest = {'version': 'test', 'sha256': {k: hashlib.sha256(v).hexdigest() for k, v in entries.items()}}
        entries[MANIFEST] = json.dumps(manifest).encode()
        path = self.root / 'test.zip'
        with zipfile.ZipFile(path, 'w') as archive:
            for name, data in entries.items():
                archive.writestr(name, data)
        return path

    def test_valid_and_missing_files(self):
        verify(self.package(), 'test', self.root)
        for missing in ('app/workbook-replay.settings.json', 'app/templates/IR_슬라이싱_수동계산.xlsx', 'runtime/Lib/site-packages/PIL/_imaging.pyd',
                        'runtime/Lib/site-packages/pillow-13.0.0.dist-info/licenses/LICENSE'):
            with self.subTest(missing=missing), self.assertRaisesRegex(ValueError, 'missing'):
                verify(self.package(lambda entries: entries.pop(missing)), 'test', self.root)

    def test_corrupt_manifest(self):
        path = self.package()
        with zipfile.ZipFile(path) as archive:
            entries = {name: archive.read(name) for name in archive.namelist()}
        manifest = json.loads(entries[MANIFEST])
        manifest['sha256']['IRFeedback.exe'] = '0' * 64
        entries[MANIFEST] = json.dumps(manifest).encode()
        with zipfile.ZipFile(path, 'w') as archive:
            for name, data in entries.items():
                archive.writestr(name, data)
        with self.assertRaisesRegex(ValueError, 'manifest hash mismatch'):
            verify(path, 'test', self.root)

    def test_unsafe_path_and_override_load_path(self):
        with self.assertRaisesRegex(ValueError, 'unsafe ZIP path'):
            verify(self.package(lambda e: e.update({'../escape': b'x'})), 'test', self.root)
        with self.assertRaisesRegex(ValueError, 'load path'):
            verify(self.package(lambda e: e.update({'runtime/python314._pth': b'python314.zip\n.'})), 'test', self.root)

if __name__ == '__main__':
    unittest.main()
