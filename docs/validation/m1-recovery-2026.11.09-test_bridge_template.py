"""Offline tamper checks for the exact bridge-verification code embedded in the template.

Uses only local build evidence. All gh calls are mocked; no publication occurs.
"""
from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile

HERE = Path(__file__).resolve().parent
SOURCE = Path('/workspace/IR_feedback_m1_recovery_nov9')
RAW = (HERE.parent / 'bridge-build-artifact.zip').read_bytes()
DIGEST = hashlib.sha256(RAW).hexdigest()
TEMPLATE = (HERE / 'publish_m1_recovery_workflow.template.yml').read_text()
section = TEMPLATE.split('      - name: Verify pinned native bridge build provenance\n', 1)[1].split('      - name:', 1)[0]
script = section.split("          @'\n", 1)[1].split("          '@ | python -", 1)[0]
SCRIPT = '\n'.join(line[10:] for line in script.splitlines())


def metadata():
    return {'id': 13661787112, 'name': 'm1-recovery-bridge-6f378742c99eaf4d16ac760183612eaf1581806d',
            'expired': False, 'size_in_bytes': len(RAW), 'digest': 'sha256:' + DIGEST,
            'workflow_run': {'id': 42592342935, 'head_sha': '6f378742c99eaf4d16ac760183612eaf1581806d',
                             'repository_id': 1434868115, 'head_repository_id': 1434868115}}


def rewrite_zip(change):
    with zipfile.ZipFile(io.BytesIO(RAW)) as original:
        data = {n: original.read(n) for n in original.namelist()}
    change(data)
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
        for name, content in data.items():
            z.writestr(name, content)
    return out.getvalue()


class BridgeTemplateTests(unittest.TestCase):
    def verify(self, meta=None, raw=RAW, repin=False, source_change=None):
        meta = deepcopy(meta or metadata())
        script = SCRIPT
        if repin:
            # Exercise inner checks even when an attacker presents a consistently
            # repinned outer ZIP and corresponding metadata.
            digest = hashlib.sha256(raw).hexdigest()
            script = script.replace(DIGEST, digest).replace('24323', str(len(raw)))
            meta.update(digest='sha256:' + digest, size_in_bytes=len(raw))
        def gh(args, **kwargs):
            self.assertEqual(args[:2], ['gh', 'api'])
            return raw if args[2].endswith('/zip') else json.dumps(meta).encode()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'reviewed-source' / 'app'
            root.mkdir(parents=True)
            for source in (SOURCE / 'app').glob('UiaBridge*'):
                if source.is_file():
                    (root / source.name).write_bytes(source.read_bytes())
            (root / 'build_uia_bridge.ps1').write_bytes((SOURCE / 'app/build_uia_bridge.ps1').read_bytes())
            if source_change:
                source_change(root)
            previous = Path.cwd()
            try:
                os.chdir(tmp)
                with patch.object(subprocess, 'check_output', side_effect=gh):
                    exec(compile(script, 'embedded_bridge_check', 'exec'), {})
            finally:
                os.chdir(previous)

    def test_valid_actual_artifact_and_source(self):
        self.verify()

    def test_metadata_tampering_is_rejected(self):
        for key, value in [('id', 1), ('name', 'other'), ('expired', True), ('size_in_bytes', 1),
                           ('digest', 'sha256:' + '0' * 64)]:
            with self.subTest(key=key):
                m = metadata(); m[key] = value
                with self.assertRaises(AssertionError): self.verify(m)
        for key, value in [('id', 1), ('head_sha', '0' * 40), ('repository_id', 1), ('head_repository_id', 1)]:
            with self.subTest(run_key=key):
                m = metadata(); m['workflow_run'][key] = value
                with self.assertRaises(AssertionError): self.verify(m)

    def test_transport_bytes_changed(self):
        with self.assertRaises(AssertionError): self.verify(raw=RAW[:-1] + b'X')

    def test_inner_build_fields_are_checked_after_outer_repin(self):
        for key, value in [('source_commit', '0' * 40), ('build_succeeded', False),
                           ('equipment_application_approved', True), ('dll_sha256', '0' * 64)]:
            with self.subTest(key=key):
                def change(data):
                    build = json.loads(data['build.json']); build[key] = value
                    data['build.json'] = json.dumps(build).encode()
                with self.assertRaises(AssertionError): self.verify(raw=rewrite_zip(change), repin=True)

    def test_embedded_dll_and_sources_are_checked_after_outer_repin(self):
        for name in ('UiaBridge.dll', 'UiaBridge.cs', 'build_uia_bridge.ps1'):
            with self.subTest(name=name):
                with self.assertRaises(AssertionError):
                    self.verify(raw=rewrite_zip(lambda d: d.update({name: d[name] + b'tampered'})), repin=True)

    def test_candidate_dll_and_source_tampering_are_rejected(self):
        for name in ('UiaBridge.dll', 'UiaBridge.cs', 'build_uia_bridge.ps1'):
            with self.subTest(name=name):
                def change(root):
                    path = root / name; path.write_bytes(path.read_bytes() + b'tampered')
                with self.assertRaises(AssertionError): self.verify(source_change=change)


if __name__ == '__main__':
    unittest.main()
