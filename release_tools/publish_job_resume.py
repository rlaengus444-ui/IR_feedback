"""Deliver the exact independently reviewed evaluation artifact; never build it."""
import hashlib,io,json,os,re,subprocess,sys,zipfile
from pathlib import Path
REPO='rlaengus444-ui/IR_feedback'
SOURCE='a2b260654c7620eca87ea57dc7fa36d3c2c24a4d'
VERSION='2026.11.11-job-resume.1'
ARTIFACT=13840961578
RUN=42988067040
OUTER_SHA='de183960938782fc24bf69988f955209e923cd199b554056b07bcd60a6440cfc'
OUTER_BYTES=34970885
ZIP_SHA='cd65db88bffb2159a7d0d04c523373e75c765dc02f3d7744c9ac37466dcc68a7'
ZIP_BYTES=35407375
RELEASE=441431537
REVIEW_SHA='57b697ed427a643c904e535d0dc73771d4dfc856438b40109aa281bfb185a713'
NOTES_SHA='f162f7a99aa7ccd2e0434394e713c6dd14249dfe15d348d0938d53dfa4448d07'
CHECKS={'build','tests','smoke','bridge','csv_guard','native_discovery','slice_perf','settings_recovery','background','hidden_recovery','hidden_session','job_resume','excel','reverify'}
def require(condition,message):
    if not condition:raise ValueError(message)
def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def api(endpoint,*,method=None,body=None,binary=False):
    cmd=['gh','api',endpoint]
    if method:cmd+=['--method',method]
    if body is not None:cmd+=['--input','-']
    result=subprocess.run(cmd,input=json.dumps(body).encode() if body is not None else None,capture_output=True,check=True,timeout=90)
    return result.stdout if binary else json.loads(result.stdout)
def check_review(r):
    require(r.get('decision')=='APPROVED_FOR_EXACT_EVALUATION_DISTRIBUTION_ONLY' and r.get('evaluation_distribution_approved') is True,'Evaluation review missing')
    require(all(r.get(k) is False for k in ('stable_release_approved','final_algorithm_approved','equipment_application_approved')),'Approval scope changed')
    require(r['source']['commit']==SOURCE and r['source']['working_tree_clean'] is True and r['source']['unresolved_source_findings']==[],'Source review mismatch')
    a=r['approved_artifact'];require((a['filename'],a['version'],a['bytes'],a['sha256'])==(f'IRFeedback-{VERSION}.zip',VERSION,ZIP_BYTES,ZIP_SHA),'Artifact review mismatch')
    require(r['review_route']['actual_design_source_and_artifact_review_completed'] is True,'Actual final review incomplete')
    c=r['ci_artifact'];require((c['workflow_run'],c['api_artifact_id_parent_supplied'],c['outer_bytes'],c['outer_sha256'])==(RUN,ARTIFACT,OUTER_BYTES,OUTER_SHA),'CI review mismatch')
def check_evidence(folder):
    c=json.loads((folder/'candidate.json').read_text(encoding='utf-8-sig'))
    require((c['source_commit'],c['version'],c['zip_sha256'])==(SOURCE,VERSION,ZIP_SHA),'Candidate metadata mismatch')
    r=json.loads((folder/'readiness.json').read_text(encoding='utf-8-sig'))
    require(r['source_commit']==SOURCE and set(r['checks'])==CHECKS,'Readiness schema mismatch')
    require(all(v=='success' for k,v in r['checks'].items() if k!='excel') and r['checks']['excel']=='failure','Unexpected CI outcomes')
    require(all(r[k] is False for k in ('automated_checks_passed','release_ready','device_application_approved')) and r['final_sol_review']=='pending','Raw holds modified')
    e=json.loads((folder/'excel.json').read_text(encoding='utf-8-sig'))
    require(e.get('excel_com_tested') is False and e.get('reason')=='Excel COM is not registered','Excel failure differs')
    tests=(folder/'tests.txt').read_text(encoding='utf-8-sig')
    require(re.search(r'Ran 549 tests in [\d.]+s\s+OK\s*$',tests) is not None,'Final Windows tests did not pass')
    native=json.loads((folder/'job-resume.json').read_text(encoding='utf-8-sig'))
    require(native.get('passed') is True and len(native['cases'])==2 and {x['machine'] for x in native['cases']}=={'M1','S3'},'Native session evidence incomplete')
    for x in native['cases']:
        require(x['passed'] and x['worker_retired'] and x['original_pid']!=x['replacement_pid'] and x['deadline_expiry_recovered_without_epoch_reset'] is True and x['single_save_confirmed_after_500ms_ui_work'] is True,'Native behavior missing')
    confirmations=native['save_confirmation_cases']
    require(len(confirmations)==4 and {(x['machine'],x['confirmation_case']) for x in confirmations}=={(m,c) for m in ('M1','S3') for c in ('long-delay','exit')},'Confirmation cases missing')
    for x in confirmations:
        require(x['passed'] and x['uncertainty_sticky'] and x['one_save_per_attempt'] and [v['delay_ms'] for v in x['short_delay_successes']]==[250,600] and [v['save_count'] for v in x['short_delay_successes']]==[1,2],'Save confirmation invalid')
    guard=json.loads((folder/'csv-guard.json').read_text(encoding='utf-8-sig'))
    p=guard['post_edit_deadline_composition']
    require(guard['passed'] and p['used_production_guard'] and p['ui_edit_simulated'] and p['deadline_wait'] and p['edit_marker'] and not p['save_marker'] and p['actual_ui_actions']==0,'Deadline composition missing')
def check_release(r,draft):
    require((r['id'],r['tag_name'],r['target_commitish'],r['draft'],r['prerelease'])==(RELEASE,VERSION,SOURCE,draft,True),'Unexpected release target or scope')
def check_tag(*,must_exist=False):
    try:
        ref=api(f'repos/{REPO}/git/ref/tags/{VERSION}')
    except subprocess.CalledProcessError as exc:
        failure=json.loads(exc.stdout)
        require(not must_exist and str(failure.get('status'))=='404','Unable to validate release tag')
        return
    obj=ref['object']
    for _ in range(5):
        require(obj['type'] in ('tag','commit') and re.fullmatch('[0-9a-f]{40}',obj['sha']) is not None,'Invalid release tag object')
        if obj['type']=='commit':
            require(obj['sha']==SOURCE,'Release tag points to unreviewed source')
            return
        obj=api(f"repos/{REPO}/git/tags/{obj['sha']}")['object']
    raise ValueError('Release tag chain exceeds bound')
def publish(source,folder,review,notes):
    require(digest(review)==REVIEW_SHA and digest(notes)==NOTES_SHA,'Approved review or notes bytes changed')
    record=json.loads(review.read_text(encoding='utf-8'))
    check_review(record)
    metadata=api(f'repos/{REPO}/actions/artifacts/{ARTIFACT}')
    require(metadata['id']==ARTIFACT and metadata['name']=='slice-candidate-'+SOURCE and metadata['expired'] is False and metadata['size_in_bytes']==OUTER_BYTES and metadata['digest']=='sha256:'+OUTER_SHA,'Immutable artifact metadata mismatch')
    w=metadata['workflow_run'];require(w['id']==RUN and w['head_sha']==SOURCE and w['repository_id']==w['head_repository_id']==1434868115,'Artifact origin mismatch')
    raw=api(f'repos/{REPO}/actions/artifacts/{ARTIFACT}/zip',binary=True)
    require(len(raw)==OUTER_BYTES and hashlib.sha256(raw).hexdigest()==OUTER_SHA,'Artifact download mismatch')
    folder.mkdir(parents=True,exist_ok=False)
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        names=archive.namelist()
        require(len(names)==len(set(names))==23 and all('/' not in n and '\\' not in n and ':' not in n and n not in ('.','..') for n in names) and archive.testzip() is None,'Unsafe artifact entries')
        archive.extractall(folder)
    check_evidence(folder)
    package=folder/f'IRFeedback-{VERSION}.zip'
    require(package.stat().st_size==ZIP_BYTES and digest(package)==ZIP_SHA,'Reviewed ZIP changed')
    sys.path.insert(0,str(source/'release_tools'))
    from verify_package import verify
    verify(package,VERSION)
    checksum=folder/'SHA256.txt'
    require(checksum.read_text(encoding='utf-8-sig').strip()==f'{ZIP_SHA}  {package.name}','Checksum mismatch')
    target=api(f'repos/{REPO}/releases/{RELEASE}')
    check_release(target,True)
    require(target['assets']==[],'Draft assets already exist; refusing overwrite')
    require(target['body']==notes.read_text(encoding='utf-8'),'Draft notes changed')
    check_tag()
    assets=[package,checksum,review]
    subprocess.run(['gh','release','upload',VERSION,*map(str,assets),'--repo',REPO],check=True,timeout=120)
    saved=api(f'repos/{REPO}/releases/{RELEASE}')
    check_release(saved,True)
    expected={p.name:p for p in assets};require(len(saved['assets'])==len(expected) and {a['name'] for a in saved['assets']}==set(expected),'Uploaded asset set differs')
    for a in saved['assets']:
        p=expected[a['name']]
        require(a['state']=='uploaded' and a['size']==p.stat().st_size and a['digest']=='sha256:'+digest(p),'Uploaded bytes differ')
    require(digest(review)==REVIEW_SHA and digest(notes)==NOTES_SHA and saved['body']==notes.read_text(encoding='utf-8'),'Approved review or release notes changed before publication')
    check_tag()
    published=api(f'repos/{REPO}/releases/{RELEASE}',method='PATCH',body={'draft':False,'prerelease':True,'make_latest':'false'})
    check_release(published,False)
    check_tag(must_exist=True)
    print(json.dumps({'published':True,'release_id':RELEASE,'source':SOURCE,'sha256':ZIP_SHA,'url':published['html_url'],'equipment_application_approved':False}))
if __name__=='__main__':
    root=Path(__file__).resolve().parents[1]
    publish(Path(sys.argv[1]).resolve(),Path(os.environ['RUNNER_TEMP'])/'reviewed-job-resume',root/'release_tools/job_resume_review/release-review-save-ack-final.json',root/'release_tools/job_resume_review/release-notes.txt')
