"""Final constants review: actual bytes and read-only metadata; writes mocked."""
import ast,contextlib,copy,importlib.util,io,json,shutil,subprocess,sys,tempfile,types,unittest
from pathlib import Path
from unittest.mock import patch

ROOT=Path('/workspace/IR_feedback_save_resume_delivery')
EV=Path('/workspace/ir-save-auto-resume-2026.11.11')
def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    result=importlib.util.module_from_spec(spec);spec.loader.exec_module(result);return result
p=load('exact_save_publisher',ROOT/'release_tools/publish_save_resume.py')
previous=load('previous_review_harness',Path('/workspace/ir-job-pid-resume-2026.11.11/publisher_review_tests.py'))
previous.p=p;previous.RAW=(EV/'final-ci.zip').read_bytes()
LIVE=json.loads((EV/'publisher-final-readonly-github.json').read_text())
META=json.loads((EV/'final-github-metadata-reviewer.json').read_text())['artifacts'][0]


class FinalPublisherTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='final-save-delivery-review-');self.addCleanup(self.tmp.cleanup)
        self.work=Path(self.tmp.name)
        self.review=self.work/'release-review-save-auto-resume-final.json';self.notes=self.work/'release-notes.txt'
        for path in (self.review,self.notes):shutil.copyfile(ROOT/'release_tools/save_resume_review'/path.name,path)
        self.g=previous.Gateway();self.g.metadata=copy.deepcopy(META)
        self.g.release={k:copy.deepcopy(v) for k,v in LIVE.items() if k!='tag_read'}
        self.g.tag_values=[previous.http_error(404),previous.http_error(404),{'object':{'type':'commit','sha':p.SOURCE}}]

    def invoke(self,real=False):
        verifier=types.ModuleType('verify_package');verifier.verify=lambda *args:None
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(p,'api',self.g.api));stack.enter_context(patch.object(p.subprocess,'run',self.g.run))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            if not real:stack.enter_context(patch.dict(sys.modules,{'verify_package':verifier}))
            p.publish(ROOT,self.work/'artifact',self.review,self.notes)

    def test_actual_exact_artifact_package_and_metadata_positive(self):
        self.invoke(real=True)
        self.assertEqual(self.g.writes(),['UPLOAD','PATCH'])
        self.assertEqual(self.g.tag_reads,3)
        self.assertEqual({x['name'] for x in self.g.release['assets']},{f'IRFeedback-{p.VERSION}.zip','SHA256.txt',self.review.name})
        self.assertEqual(p.digest(self.work/'artifact'/f'IRFeedback-{p.VERSION}.zip'),p.ZIP_SHA)

    def test_final_constants_and_live_draft_scope(self):
        self.assertEqual(p.ARTIFACT,META['id']);self.assertEqual(p.RUN,META['workflow_run']['id'])
        self.assertEqual(p.SOURCE,META['workflow_run']['head_sha']);self.assertEqual('sha256:'+p.OUTER_SHA,META['digest'])
        self.assertEqual(p.OUTER_BYTES,META['size_in_bytes'])
        self.assertEqual(p.digest(self.review),p.REVIEW_SHA);self.assertEqual(p.digest(self.notes),p.NOTES_SHA)
        p.check_release(self.g.release,True)
        self.assertEqual(self.g.release['assets'],[]);self.assertEqual(self.g.release['body'],self.notes.read_text())
        self.assertEqual(LIVE['tag_read']['error_data']['status'],'404')
        p.check_review(json.loads(self.review.read_text()));p.check_evidence(EV/'final-ci')

    def test_changed_approval_stops_before_api(self):
        self.review.write_bytes(self.review.read_bytes()+b'\n')
        with self.assertRaises(ValueError):self.invoke()
        self.assertEqual(self.g.calls,[])

    def test_wrong_source_metadata_stops_before_upload(self):
        self.g.metadata['workflow_run']['head_sha']='0'*40
        with self.assertRaises(ValueError):self.invoke()
        self.assertEqual(self.g.writes(),[])

    def test_changed_draft_target_stops_before_upload(self):
        self.g.release['target_commitish']='0'*40
        with self.assertRaises(ValueError):self.invoke()
        self.assertEqual(self.g.writes(),[])

    def test_wrong_tag_stops_before_upload(self):
        self.g.tag_values=[{'object':{'type':'commit','sha':'0'*40}}]
        with self.assertRaises(ValueError):self.invoke()
        self.assertEqual(self.g.writes(),[])

    def test_uploaded_digest_changed_prevents_public_patch(self):
        self.g.saved_change=lambda r:r['assets'][0].update(digest='sha256:'+'0'*64)
        with self.assertRaises(ValueError):self.invoke()
        self.assertEqual(self.g.writes(),['UPLOAD'])

    def test_saved_notes_changed_prevents_public_patch(self):
        self.g.saved_change=lambda r:r.update(body='changed')
        with self.assertRaises(ValueError):self.invoke()
        self.assertEqual(self.g.writes(),['UPLOAD'])

    def test_tag_changed_after_upload_prevents_public_patch(self):
        self.g.tag_values=[{'object':{'type':'commit','sha':p.SOURCE}},{'object':{'type':'commit','sha':'0'*40}}]
        with self.assertRaises(ValueError):self.invoke()
        self.assertEqual(self.g.writes(),['UPLOAD'])


if __name__=='__main__':unittest.main(verbosity=2)
