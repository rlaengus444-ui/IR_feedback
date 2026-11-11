"""Independent offline review checks. Every GitHub call is intercepted.

Uses immutable real final artifact for the positive pipeline; no credentials,
network, uploads, or modifications to protected review/evidence/source files.
"""
import contextlib
import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path('/workspace/IR_feedback_job_resume_delivery')
EVIDENCE = Path('/workspace/ir-job-pid-resume-2026.11.11')
spec = importlib.util.spec_from_file_location('publisher_under_review', ROOT/'release_tools/publish_job_resume.py')
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)
RAW = (EVIDENCE/'ack-final-ci.zip').read_bytes()


class Gateway:
    def __init__(self):
        self.calls = []
        self.uploaded = False
        self.tag_reads = 0
        self.tag_values = []
        self.tag_objects = {}
        self.saved_change = lambda r: None
        self.upload_change = lambda: None
        self.upload_error = None
        self.download = RAW
        self.metadata = {'id': p.ARTIFACT, 'name': 'slice-candidate-'+p.SOURCE,
                         'expired': False, 'size_in_bytes': p.OUTER_BYTES,
                         'digest': 'sha256:'+p.OUTER_SHA,
                         'workflow_run': {'id': p.RUN, 'head_sha': p.SOURCE,
                                          'repository_id': 1434868115,
                                          'head_repository_id': 1434868115}}
        self.release = {'id': p.RELEASE, 'tag_name': p.VERSION,
                        'target_commitish': p.SOURCE, 'draft': True,
                        'prerelease': True, 'assets': [],
                        'body': (ROOT/'release_tools/job_resume_review/release-notes.txt').read_text(),
                        'html_url': 'https://example.invalid/review-only'}

    def api(self, endpoint, *, method=None, body=None, binary=False):
        self.calls.append((method or 'GET', endpoint, copy.deepcopy(body)))
        prefix = f'repos/{p.REPO}/'
        if endpoint == prefix+f'actions/artifacts/{p.ARTIFACT}':
            return copy.deepcopy(self.metadata)
        if endpoint == prefix+f'actions/artifacts/{p.ARTIFACT}/zip':
            assert binary
            return self.download
        if endpoint == prefix+f'git/ref/tags/{p.VERSION}':
            index = self.tag_reads
            self.tag_reads += 1
            value = self.tag_values[min(index, len(self.tag_values)-1)] if self.tag_values else {'object': {'type': 'commit', 'sha': p.SOURCE}}
            if isinstance(value, Exception):
                raise value
            return copy.deepcopy(value)
        if endpoint.startswith(prefix+'git/tags/'):
            return copy.deepcopy(self.tag_objects[endpoint.rsplit('/', 1)[1]])
        if endpoint == prefix+f'releases/{p.RELEASE}':
            if method == 'PATCH':
                assert self.uploaded
                assert body == {'draft': False, 'prerelease': True, 'make_latest': 'false'}
                self.release.update(draft=False, prerelease=True)
                return copy.deepcopy(self.release)
            value = copy.deepcopy(self.release)
            if self.uploaded:
                self.saved_change(value)
            return value
        raise AssertionError('Unmocked endpoint: '+endpoint)

    def run(self, command, **kwargs):
        assert command[:3] == ['gh', 'release', 'upload'], command
        assert command[3] == p.VERSION
        assert command[-2:] == ['--repo', p.REPO]
        assert '--clobber' not in command
        assert kwargs == {'check': True, 'timeout': 120}
        self.calls.append(('UPLOAD', tuple(command), None))
        if self.upload_error:
            raise self.upload_error
        self.uploaded = True
        self.release['assets'] = [
            {'name': Path(v).name, 'state': 'uploaded', 'size': Path(v).stat().st_size,
             'digest': 'sha256:'+p.digest(v)} for v in command[4:-2]]
        self.upload_change()
        return subprocess.CompletedProcess(command, 0)

    def writes(self):
        return [c[0] for c in self.calls if c[0] in ('UPLOAD', 'PATCH')]


def http_error(status):
    return subprocess.CalledProcessError(1, ['gh', 'api', 'mock'],
                                         output=json.dumps({'status': str(status)}).encode())


class PublisherReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='publisher-review-')
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.review = self.work/'release-review-save-ack-final.json'
        self.notes = self.work/'release-notes.txt'
        for path in (self.review, self.notes):
            shutil.copyfile(ROOT/'release_tools/job_resume_review'/path.name, path)
        self.gateway = Gateway()

    def invoke(self, *, real_verifier=False):
        # Only the positive pipeline imports the real complete package verifier.
        # Every API and subprocess route is replaced, including unforeseen writes.
        verifier = types.ModuleType('verify_package')
        verifier.verify = lambda *args: None
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(p, 'api', self.gateway.api))
            stack.enter_context(patch.object(p.subprocess, 'run', self.gateway.run))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            if not real_verifier:
                stack.enter_context(patch.dict(sys.modules, {'verify_package': verifier}))
            p.publish(ROOT, self.work/'artifact', self.review, self.notes)

    def reject_before_upload(self):
        with self.assertRaises((ValueError, KeyError, subprocess.CalledProcessError)):
            self.invoke()
        self.assertEqual(self.gateway.writes(), [])

    def test_actual_exact_artifact_positive_real_package_verifier(self):
        self.invoke(real_verifier=True)
        self.assertEqual(self.gateway.writes(), ['UPLOAD', 'PATCH'])
        self.assertEqual(self.gateway.tag_reads, 3)
        names = {a['name'] for a in self.gateway.release['assets']}
        self.assertEqual(names, {f'IRFeedback-{p.VERSION}.zip', 'SHA256.txt', self.review.name})
        self.assertEqual(p.digest(self.work/'artifact'/f'IRFeedback-{p.VERSION}.zip'), p.ZIP_SHA)

    def test_review_bytes_tamper_before_any_api(self):
        self.review.write_bytes(self.review.read_bytes()+b'\n')
        self.reject_before_upload()
        self.assertEqual(self.gateway.calls, [])

    def test_notes_bytes_tamper_before_any_api(self):
        self.notes.write_bytes(self.notes.read_bytes()+b'\n')
        self.reject_before_upload()
        self.assertEqual(self.gateway.calls, [])

    def test_metadata_provenance_each_mismatch_before_upload(self):
        mutations = [lambda m: m.update(id=1), lambda m: m.update(name='other'),
                     lambda m: m.update(expired=True), lambda m: m.update(size_in_bytes=1),
                     lambda m: m.update(digest='sha256:'+'0'*64),
                     lambda m: m['workflow_run'].update(id=1),
                     lambda m: m['workflow_run'].update(head_sha='0'*40),
                     lambda m: m['workflow_run'].update(repository_id=1),
                     lambda m: m['workflow_run'].update(head_repository_id=1)]
        for mutate in mutations:
            with self.subTest(mutate=mutations.index(mutate)):
                self.gateway = Gateway()
                mutate(self.gateway.metadata)
                self.reject_before_upload()

    def test_changed_artifact_body_before_upload(self):
        self.gateway.download = RAW[:-1]+bytes([RAW[-1]^1])
        self.reject_before_upload()

    def test_draft_each_mismatch_before_upload(self):
        for key, value in [('id', 1), ('tag_name', 'other'), ('target_commitish', '0'*40),
                           ('draft', False), ('prerelease', False), ('assets', [{}]),
                           ('body', 'unreviewed')]:
            with self.subTest(key=key):
                self.gateway = Gateway()
                self.gateway.release[key] = value
                if (self.work/'artifact').exists():
                    shutil.rmtree(self.work/'artifact')
                self.reject_before_upload()

    def test_wrong_tag_before_upload(self):
        self.gateway.tag_values = [{'object': {'type': 'commit', 'sha': '0'*40}}]
        self.reject_before_upload()

    def test_annotated_tag_supported(self):
        sha = '1'*40
        self.gateway.tag_values = [{'object': {'type': 'tag', 'sha': sha}}]
        self.gateway.tag_objects[sha] = {'object': {'type': 'commit', 'sha': p.SOURCE}}
        self.invoke()
        self.assertEqual(self.gateway.writes(), ['UPLOAD', 'PATCH'])

    def test_annotated_wrong_commit_before_upload(self):
        sha = '1'*40
        self.gateway.tag_values = [{'object': {'type': 'tag', 'sha': sha}}]
        self.gateway.tag_objects[sha] = {'object': {'type': 'commit', 'sha': '0'*40}}
        self.reject_before_upload()

    def test_annotation_loop_is_bounded_before_upload(self):
        sha = '1'*40
        self.gateway.tag_values = [{'object': {'type': 'tag', 'sha': sha}}]
        self.gateway.tag_objects[sha] = {'object': {'type': 'tag', 'sha': sha}}
        self.reject_before_upload()
        self.assertEqual(sum('/git/tags/' in c[1] for c in self.gateway.calls), 5)

    def test_missing_tag_allowed_only_until_publication(self):
        self.gateway.tag_values = [http_error(404), http_error(404),
                                   {'object': {'type': 'commit', 'sha': p.SOURCE}}]
        self.invoke()
        self.assertEqual(self.gateway.writes(), ['UPLOAD', 'PATCH'])

    def test_non404_tag_error_before_upload(self):
        for status in (401, 403, 451, 500):
            with self.subTest(status=status):
                self.gateway = Gateway()
                self.gateway.tag_values = [http_error(status)]
                if (self.work/'artifact').exists():
                    shutil.rmtree(self.work/'artifact')
                self.reject_before_upload()

    def test_wrong_tag_after_upload_prevents_publication(self):
        self.gateway.tag_values = [{'object': {'type': 'commit', 'sha': p.SOURCE}},
                                   {'object': {'type': 'commit', 'sha': '0'*40}}]
        with self.assertRaises(ValueError): self.invoke()
        self.assertEqual(self.gateway.writes(), ['UPLOAD'])

    def test_saved_draft_or_assets_changed_prevents_publication(self):
        changes = [lambda r: r.update(draft=False), lambda r: r.update(body='changed'),
                   lambda r: r.update(assets=[]),
                   lambda r: r['assets'][0].update(digest='sha256:'+'0'*64),
                   lambda r: r['assets'][0].update(state='new'),
                   lambda r: r['assets'][0].update(size=1)]
        for index, mutate in enumerate(changes):
            with self.subTest(index=index):
                self.gateway = Gateway()
                self.gateway.saved_change = mutate
                if (self.work/'artifact').exists(): shutil.rmtree(self.work/'artifact')
                with self.assertRaises(ValueError): self.invoke()
                self.assertEqual(self.gateway.writes(), ['UPLOAD'])

    def test_changed_local_approved_bytes_after_upload_prevents_publication(self):
        for path in (self.review, self.notes):
            with self.subTest(path=path.name):
                self.gateway = Gateway()
                original = path.read_bytes()
                self.gateway.upload_change = lambda: path.write_bytes(original+b'\n')
                if (self.work/'artifact').exists(): shutil.rmtree(self.work/'artifact')
                with self.assertRaises(ValueError): self.invoke()
                self.assertEqual(self.gateway.writes(), ['UPLOAD'])
                path.write_bytes(original)

    def test_failed_upload_never_publishes(self):
        self.gateway.upload_error = subprocess.CalledProcessError(1, ['gh', 'release', 'upload'])
        with self.assertRaises(subprocess.CalledProcessError): self.invoke()
        self.assertEqual(self.gateway.writes(), ['UPLOAD'])

    def test_postpublication_missing_tag_is_detected(self):
        self.gateway.tag_values = [{'object': {'type': 'commit', 'sha': p.SOURCE}},
                                   {'object': {'type': 'commit', 'sha': p.SOURCE}}, http_error(404)]
        with self.assertRaises(ValueError): self.invoke()
        self.assertEqual(self.gateway.writes(), ['UPLOAD', 'PATCH'])

    def test_actual_evidence_accepts_and_rejects_mutated_holds(self):
        directory = self.work/'evidence'
        directory.mkdir()
        for name in ('candidate.json','readiness.json','excel.json','tests.txt','job-resume.json','csv-guard.json'):
            shutil.copyfile(EVIDENCE/'ack-final-ci'/name, directory/name)
        p.check_evidence(directory)
        path = directory/'readiness.json'
        original = path.read_bytes()
        for key, value in [('release_ready', True), ('final_sol_review', 'approved'),
                           ('automated_checks_passed', True), ('device_application_approved', True)]:
            with self.subTest(key=key):
                data = json.loads(original)
                data[key] = value
                path.write_text(json.dumps(data))
                with self.assertRaises(ValueError): p.check_evidence(directory)
        path.write_bytes(original)
        (directory/'tests.txt').write_text('Ran 549 tests in 1.0s\nFAILED (failures=1)\n')
        with self.assertRaises(ValueError): p.check_evidence(directory)

    def test_workflow_has_no_rebuild_exact_source_manual_only(self):
        text = (ROOT/'.github/workflows/publish-csv6.yml').read_text()
        self.assertIn('workflow_dispatch:', text)
        self.assertIn('ref: ${{ github.sha }}', text)
        self.assertIn('ref: '+p.SOURCE, text)
        self.assertIn('core.autocrlf false', text)
        self.assertIn('contents: write', text)
        self.assertIn('actions: read', text)
        self.assertIn('GH_TOKEN: ${{ github.token }}', text)
        for forbidden in ('push:', 'pull_request:', '--clobber', 'build_', 'upload-artifact', 'download-artifact'):
            self.assertNotIn(forbidden, text)


if __name__ == '__main__':
    unittest.main(verbosity=2)
