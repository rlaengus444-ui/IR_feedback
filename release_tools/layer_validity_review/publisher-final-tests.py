"""Exact final publisher review. Actual immutable bytes; all API/writes mocked."""
import contextlib
import copy
import importlib.util
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path('/workspace/IR_feedback_layer_delivery')
SOURCE_ROOT = Path('/workspace/IR_feedback')
EV = Path('/workspace/layer-validity-2026.11.12')
spec = importlib.util.spec_from_file_location('exact_layer_publisher', ROOT / 'release_tools/publish_layer_validity.py')
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)
RAW = (EV / 'replacement-ci.zip').read_bytes()
META = json.loads((EV / 'reviewer-final-github-metadata.json').read_text())['artifacts']['artifacts'][0]
LIVE = json.loads((EV / 'publisher-final-readonly-github.json').read_text())


class Gateway:
    def __init__(self):
        self.metadata = copy.deepcopy(META)
        self.release = copy.deepcopy(LIVE['release'])
        self.raw = RAW
        self.calls = []
        self.tags = [copy.deepcopy(LIVE['tag'])]
        self.tag_reads = 0
        self.release_reads = 0
        self.saved_change = None

    def writes(self):
        return [x for x in self.calls if x in ('UPLOAD', 'PATCH')]

    def api(self, endpoint, *, method=None, body=None, binary=False):
        self.calls.append(endpoint)
        prefix = f'repos/{p.REPO}/'
        if endpoint == prefix + f'actions/artifacts/{p.ARTIFACT}' and method is None:
            return copy.deepcopy(self.metadata)
        if endpoint == prefix + f'actions/artifacts/{p.ARTIFACT}/zip' and method is None:
            assert binary
            return self.raw
        if endpoint == prefix + f'git/ref/tags/{p.VERSION}' and method is None:
            self.tag_reads += 1
            value = self.tags.pop(0) if len(self.tags) > 1 else self.tags[0]
            return copy.deepcopy(value)
        if endpoint == prefix + f'releases/{p.RELEASE}' and method is None:
            self.release_reads += 1
            if self.release_reads == 2 and self.saved_change:
                self.saved_change(self.release)
            return copy.deepcopy(self.release)
        if endpoint == prefix + f'releases/{p.RELEASE}' and method == 'PATCH':
            assert body == {'draft': False, 'prerelease': True, 'make_latest': 'false'}
            self.calls.append('PATCH')
            self.release.update(draft=False, prerelease=True)
            return copy.deepcopy(self.release)
        raise AssertionError('Unexpected API call: ' + endpoint)

    def run(self, command, *, check, timeout):
        assert command[:4] == ['gh', 'release', 'upload', p.VERSION]
        assert command[-2:] == ['--repo', p.REPO]
        assert '--clobber' not in command and check and timeout == 120
        paths = [Path(name) for name in command[4:-2]]
        assert len(paths) == 3
        assert {x.name for x in paths} == {f'IRFeedback-{p.VERSION}.zip', 'SHA256.txt', 'release-review-layer-validity-final.json'}
        self.calls.append('UPLOAD')
        self.release['assets'] = [dict(name=x.name, size=x.stat().st_size, digest='sha256:' + p.digest(x), state='uploaded') for x in paths]
        return types.SimpleNamespace(returncode=0)


class FinalPublisherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='final-layer-publisher-review-')
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name)
        self.review = self.work / 'release-review-layer-validity-final.json'
        self.notes = self.work / 'release-notes.txt'
        for target in (self.review, self.notes):
            shutil.copyfile(ROOT / 'release_tools/layer_validity_review' / target.name, target)
        self.g = Gateway()

    def invoke(self, *, real_verifier=False):
        mock_verifier = types.ModuleType('verify_package')
        mock_verifier.verify = lambda *args: None
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(p, 'api', self.g.api))
            stack.enter_context(patch.object(p.subprocess, 'run', self.g.run))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            if not real_verifier:
                stack.enter_context(patch.dict(sys.modules, {'verify_package': mock_verifier}))
            p.publish(SOURCE_ROOT, self.work / 'artifact', self.review, self.notes)

    def test_actual_complete_path_exact_archive_real_verifier(self):
        self.invoke(real_verifier=True)
        self.assertEqual(['UPLOAD', 'PATCH'], self.g.writes())
        self.assertEqual(3, self.g.tag_reads)
        self.assertFalse(self.g.release['draft'])
        self.assertTrue(self.g.release['prerelease'])
        self.assertEqual(p.ZIP_SHA, p.digest(self.work / 'artifact' / f'IRFeedback-{p.VERSION}.zip'))

    def test_constants_match_actual_metadata_protected_review_notes_and_tag(self):
        self.assertEqual((p.ARTIFACT, p.RUN, p.SOURCE), (META['id'], META['workflow_run']['id'], META['workflow_run']['head_sha']))
        self.assertEqual((p.OUTER_BYTES, 'sha256:' + p.OUTER_SHA), (META['size_in_bytes'], META['digest']))
        self.assertEqual(p.REVIEW_SHA, p.digest(self.review))
        self.assertEqual(p.NOTES_SHA, p.digest(self.notes))
        self.assertEqual(self.review.read_bytes(), (EV / self.review.name).read_bytes())
        self.assertEqual(self.notes.read_text(), LIVE['release']['body'])
        p.check_release(LIVE['release'], True)
        p.check_review(json.loads(self.review.read_text()))
        p.check_evidence(EV / 'replacement-ci')
        self.assertEqual([], LIVE['release']['assets'])
        self.assertEqual({'type': 'commit', 'sha': p.SOURCE}, {k: LIVE['tag']['object'][k] for k in ('type', 'sha')})

    def test_changed_approval_stops_before_api(self):
        self.review.write_bytes(self.review.read_bytes() + b'\n')
        with self.assertRaises(ValueError):
            self.invoke()
        self.assertEqual([], self.g.calls)

    def test_wrong_source_artifact_metadata_stops_before_upload(self):
        self.g.metadata['workflow_run']['head_sha'] = '0' * 40
        with self.assertRaises(ValueError):
            self.invoke()
        self.assertEqual([], self.g.writes())

    def test_wrong_outer_download_stops_before_unpack_and_upload(self):
        self.g.raw = RAW + b'changed'
        with self.assertRaises(ValueError):
            self.invoke()
        self.assertFalse((self.work / 'artifact').exists())
        self.assertEqual([], self.g.writes())

    def test_changed_draft_target_stops_before_upload(self):
        self.g.release['target_commitish'] = '0' * 40
        with self.assertRaises(ValueError):
            self.invoke()
        self.assertEqual([], self.g.writes())

    def test_existing_draft_asset_refuses_overwrite(self):
        self.g.release['assets'] = [{'name': 'unexpected'}]
        with self.assertRaises(ValueError):
            self.invoke()
        self.assertEqual([], self.g.writes())

    def test_wrong_initial_tag_stops_before_upload(self):
        self.g.tags = [{'object': {'type': 'commit', 'sha': '0' * 40}}]
        with self.assertRaises(ValueError):
            self.invoke()
        self.assertEqual([], self.g.writes())

    def test_uploaded_digest_change_blocks_public_patch(self):
        self.g.saved_change = lambda r: r['assets'][0].update(digest='sha256:' + '0' * 64)
        with self.assertRaises(ValueError):
            self.invoke()
        self.assertEqual(['UPLOAD'], self.g.writes())

    def test_saved_notes_change_blocks_public_patch(self):
        self.g.saved_change = lambda r: r.update(body='changed')
        with self.assertRaises(ValueError):
            self.invoke()
        self.assertEqual(['UPLOAD'], self.g.writes())

    def test_tag_change_after_upload_blocks_public_patch(self):
        self.g.tags = [copy.deepcopy(LIVE['tag']), {'object': {'type': 'commit', 'sha': '0' * 40}}]
        with self.assertRaises(ValueError):
            self.invoke()
        self.assertEqual(['UPLOAD'], self.g.writes())

    def test_workflow_exact_source_manual_and_no_rebuild(self):
        text = (ROOT / '.github/workflows/publish-csv6.yml').read_text()
        self.assertIn('workflow_dispatch:', text)
        self.assertIn('ref: ${{ github.sha }}', text)
        self.assertIn('ref: ' + p.SOURCE, text)
        self.assertNotIn('push:', text)
        self.assertNotIn('build_slice_package', text)
        self.assertNotIn('--clobber', text)


if __name__ == '__main__':
    unittest.main(verbosity=2)
