"""Independent local publisher/workflow checks. All remote and mutation calls are mocked."""
from contextlib import redirect_stdout
from copy import deepcopy
import ast
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
from unittest.mock import patch

ROOT = Path(os.environ.get('M1_SOURCE_ROOT', '/workspace/IR_feedback_m1_reviewed_final'))
EVIDENCE = Path(os.environ.get('M1_EVIDENCE', '/workspace/ir-m1-recovery-2026.11.09/approved-ci'))
PUBLISHER = Path(__file__).with_name('publish_m1_recovery_candidate.py')
COMMIT = os.environ.get('M1_CANDIDATE_COMMIT', 'e97564fb998f31cce27328bc4e326b164869c649')
VERSION = '2026.11.09-m1-recovery.1'
RUN_ID = int(os.environ.get('M1_RUN_ID', '42601104496'))
ARCHIVE_NAME = f'IRFeedback-{VERSION}.zip'
if not (EVIDENCE / 'candidate.json').is_file() or not (EVIDENCE / ARCHIVE_NAME).is_file():
    raise SystemExit('Actual Windows evidence is not available; no mock report produced')
DIGEST = json.loads((EVIDENCE / 'candidate.json').read_text(encoding='utf-8-sig'))['zip_sha256']
PUBLISHER_DIGEST = hashlib.sha256(PUBLISHER.read_bytes()).hexdigest()
tempfile.tempdir = str(EVIDENCE.parent)
sys.path.insert(0, str(PUBLISHER.parent))
spec = importlib.util.spec_from_file_location('reviewed_publisher', PUBLISHER)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
assert m.verify(EVIDENCE / ARCHIVE_NAME, VERSION, root=ROOT) == DIGEST
BASE = {p.name: p.read_bytes() for p in EVIDENCE.iterdir() if p.name != ARCHIVE_NAME}
tree = ast.parse(PUBLISHER.read_text())
REQUIRED = next(ast.literal_eval(n.value) for n in ast.walk(tree)
                if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'required_tests' for t in n.targets))
results = []


def update_json(files, name, fn):
    data = json.loads(files[name].decode('utf-8-sig'))
    fn(data)
    files[name] = json.dumps(data).encode()


def publisher_case(name, *, files_change=None, review_change=None, options=None, should_pass=False, local_only=False):
    files = dict(BASE)
    if files_change:
        files_change(files)
    remote_files = BASE if local_only else files
    opts = options or {}
    review = dict(candidate_commit=COMMIT, candidate_zip_sha256=DIGEST, publisher_sha256=PUBLISHER_DIGEST,
                  workflow_run_id=RUN_ID, evaluation_distribution_allowed=True,
                  independent_post_review_complete=True, equipment_application_approved=False)
    if review_change:
        review.update(review_change)
    calls = []
    release = None
    with tempfile.TemporaryDirectory(prefix='sol-publisher-check-') as temp:
        base = Path(temp)
        local = base / 'local'
        local.mkdir()
        for n, data in files.items():
            (local / n).write_bytes(data)
        os.link(EVIDENCE / ARCHIVE_NAME, local / ARCHIVE_NAME)
        review_path = base / 'review.json'
        review_path.write_text(json.dumps(review))

        def checked_output(command, **kwargs):
            assert command[:3] == ['git', '-C', str(ROOT)], command
            if command[3:] == ['rev-parse', 'HEAD']:
                return opts.get('head', COMMIT) + '\n'
            if command[3:] == ['status', '--porcelain']:
                return opts.get('dirty', '')
            raise AssertionError(command)

        def run(command, **kwargs):
            assert command[0] == 'gh', command
            if command[1:3] == ['run', 'download']:
                assert command[3] == str(RUN_ID)
                assert command[command.index('--repo') + 1] == m.REPOSITORY
                assert command[command.index('--name') + 1] == f'slice-candidate-{COMMIT}'
                destination = Path(command[command.index('--dir') + 1])
                for n, data in remote_files.items():
                    (destination / n).write_bytes(data)
                os.link(EVIDENCE / ARCHIVE_NAME, destination / ARCHIVE_NAME)
                calls.append('download')
            elif command[1:3] == ['release', 'upload']:
                assert command[3] == VERSION
                assert Path(command[4]) == local / ARCHIVE_NAME
                assert Path(command[5]).read_bytes() == f'{DIGEST}  {ARCHIVE_NAME}\n'.encode()
                calls.append('upload')
            else:
                raise AssertionError(command)
            return subprocess.CompletedProcess(command, 0)

        def gh_json(endpoint, *args, data=None):
            nonlocal release
            prefix = f'repos/{m.REPOSITORY}/'
            assert endpoint.startswith(prefix), endpoint
            route = endpoint[len(prefix):]
            if route == f'actions/runs/{RUN_ID}':
                value = dict(head_sha=COMMIT, status='completed', conclusion='failure', path='.github/workflows/validate-slice.yml')
                value.update(opts.get('run', {}))
                return value
            if route == 'releases?per_page=100':
                return [{'tag_name': VERSION}] if opts.get('existing_release') else []
            if route == f'git/matching-refs/tags/{VERSION}':
                return [{'ref': 'refs/tags/' + VERSION}] if opts.get('existing_tag') else []
            if route == 'git/refs':
                assert data == {'ref': 'refs/tags/' + VERSION, 'sha': COMMIT}
                calls.append('create_tag')
                return {'ref': data['ref'], 'object': {'sha': COMMIT}}
            if route == f'git/ref/tags/{VERSION}':
                return {'ref': 'refs/tags/' + VERSION, 'object': {'sha': '0' * 40 if opts.get('changed_tag') else COMMIT, 'type': 'commit'}}
            if route == 'releases':
                assert data['draft'] is True and data['prerelease'] is True
                assert data['target_commitish'] == COMMIT and data['tag_name'] == VERSION
                assert '최종 알고리즘 승인과 실제 장비 적용은 보류합니다.' in data['body']
                assert '최종 알고리즘 승인과 실제 장비 적용은 보류합니다.' in data['body']
                release = dict(data, id=777, html_url='https://example.invalid/review-only')
                calls.append('draft')
                return deepcopy(release)
            if route == 'releases/777/assets':
                checksum = f'{DIGEST}  {ARCHIVE_NAME}\n'.encode()
                assets = [dict(name=ARCHIVE_NAME, state='uploaded', digest='sha256:' + DIGEST,
                               size=(EVIDENCE / ARCHIVE_NAME).stat().st_size),
                          dict(name='SHA256.txt', state='uploaded', digest='sha256:' + hashlib.sha256(checksum).hexdigest(), size=len(checksum))]
                if opts.get('bad_asset'):
                    assets[0]['digest'] = 'sha256:' + '0' * 64
                return assets
            if route == 'releases/777':
                if args:
                    assert args == ('-X', 'PATCH') and data == {'draft': False, 'prerelease': True, 'make_latest': 'false'}
                    release.update(draft=False)
                    calls.append('publish')
                return deepcopy(release)
            raise AssertionError((route, args, data))

        argv = [str(PUBLISHER), str(local), opts.get('candidate', COMMIT), DIGEST, str(review_path), str(ROOT)]
        failure = None
        try:
            with patch.object(sys, 'argv', argv), patch.object(m.subprocess, 'check_output', checked_output), \
                 patch.object(m.subprocess, 'run', run), patch.object(m, 'gh_json', gh_json), \
                 patch.object(m, 'verify', lambda *a, **k: opts.get('verified_digest', DIGEST)), redirect_stdout(io.StringIO()):
                m.main()
        except ValueError as exc:
            failure = str(exc)
        if should_pass:
            assert failure is None and calls[-1] == 'publish', (name, failure, calls)
        else:
            assert failure is not None and 'publish' not in calls, (name, failure, calls)
        results.append(dict(name=name, passed=True, rejected_reason=failure))


publisher_case('valid exact evaluation draft, immutable tag, asset readback and not-latest prerelease', should_pass=True)
publisher_case('wrong candidate commit', options={'candidate': '0' * 40})
publisher_case('wrong source HEAD', options={'head': '0' * 40})
publisher_case('dirty source', options={'dirty': ' M app/csv_source.py\n'})
for key, value in [('candidate_commit', '0' * 40), ('candidate_zip_sha256', '0' * 64),
                   ('publisher_sha256', '0' * 64), ('workflow_run_id', 41593781156),
                   ('evaluation_distribution_allowed', False), ('independent_post_review_complete', False),
                   ('equipment_application_approved', True)]:
    publisher_case('review gate ' + key, review_change={key: value})
for key, value in [('head_sha', '0' * 40), ('status', 'in_progress'), ('path', '.github/workflows/other.yml')]:
    publisher_case('workflow provenance ' + key, options={'run': {key: value}})
publisher_case('altered local artifact evidence', files_change=lambda f: update_json(f, 'candidate.json', lambda d: d.update(version='altered')), local_only=True)
for key in ['build', 'tests', 'smoke', 'bridge', 'csv_guard', 'native_discovery', 'slice_perf', 'settings_recovery', 'background', 'hidden_recovery', 'hidden_session', 'reverify']:
    publisher_case('Windows gate ' + key, files_change=lambda f, k=key: update_json(f, 'readiness.json', lambda d: d['checks'].update({k: 'failure'})))
for key, value in [('device_application_approved', True), ('release_ready', True), ('automated_checks_passed', True), ('final_sol_review', 'approved')]:
    publisher_case('raw equipment status ' + key, files_change=lambda f, k=key, v=value: update_json(f, 'readiness.json', lambda d: d.update({k: v})))
for filename in ['bridge.json', 'csv-guard.json', 'discovery.json', 'slice-performance.json', 'settings-recovery.json', 'background.json', 'hidden-recovery.json', 'hidden-session.json']:
    publisher_case('native report ' + filename, files_change=lambda f, n=filename: update_json(f, n, lambda d: d.update(passed=False)))
for key in ['harness_sha256', 'bridge_sha256', 'helper_sha256']:
    publisher_case('native provenance ' + key, files_change=lambda f, k=key: update_json(f, 'background.json', lambda d: d.update({k: '0' * 64})))
for key in ['bridge_sha256', 'helper_sha256']:
    publisher_case('hidden recovery provenance ' + key, files_change=lambda f, k=key: update_json(f, 'hidden-recovery.json', lambda d: d.update({k: '0' * 64})))
for key in ['initial_scope_proof', 'forged_root_scope_holds', 'unnamed_input_inventoried', 'old_hidden_failure_reproduced', 'hidden_scope_recovery', 'zero_save_or_navigation', 'full_m1_s3_both_hide_modes_two_stable_audits']:
    publisher_case('hidden recovery check ' + key, files_change=lambda f, k=key: update_json(f, 'hidden-recovery.json', lambda d: d['checks'].update({k: False})))
for key in ['probe', 'fixture', 'native_helper.ps1', 'UiaBridge.dll', 'session.py', 'observed_session.py', 'resilient_adapter.py']:
    publisher_case('hidden session provenance ' + key, files_change=lambda f, k=key: update_json(f, 'hidden-session.json', lambda d: d['source_sha256'].update({k: '0' * 64})))
publisher_case('missing hidden profile', files_change=lambda f: update_json(f, 'hidden-recovery.json', lambda d: d['full_profiles'].pop(next(iter(d['full_profiles'])))))
publisher_case('missing hidden session case', files_change=lambda f: update_json(f, 'hidden-session.json', lambda d: d['cases'].pop()))
publisher_case('session recovery unexpectedly saved', files_change=lambda f: update_json(f, 'hidden-session.json', lambda d: d['cases'][0].update(recovery_saves=1)))
publisher_case('session wrong power ramp', files_change=lambda f: update_json(f, 'hidden-session.json', lambda d: d['cases'][0].update(powers=[80])))
for key, value in [('capture_error', 'capture failed'), ('fixture_running', False), ('wrapper_outages', [{'reason': 'fault'}]),
                   ('runner_calls', []), ('native_calls', []), ('native_recoveries', []),
                   ('foreground_maintained', False), ('foreground_samples', 0), ('saves', 0),
                   ('navigation', 2), ('history_layers', []), ('history_segment', 0), ('non_ir_unchanged', False)]:
    publisher_case('pre-teardown ' + key, files_change=lambda f, k=key, v=value: update_json(
        f, 'hidden-session.json', lambda d: d['cases'][0]['readonly_pre_teardown'].update({k: v})))
for key in ['same_reader', 'same_native_adapter', 'same_observed_session', 'chart_history_preserved']:
    publisher_case('session identity ' + key, files_change=lambda f, k=key: update_json(
        f, 'hidden-session.json', lambda d: d['cases'][0].update({k: False})))
for key in ('helper_retired', 'active_helper_absent', 'unretired_helper_absent', 'helper_fence_unlocked', 'shared_fence_verified'):
    for value in (False, None):
        publisher_case('restore helper lifecycle ' + key + ' ' + str(value), files_change=lambda f, k=key, v=value: update_json(
            f, 'hidden-recovery.json', lambda d: d['helper_intervals'][0].update({k: v})))
    publisher_case('missing restore helper lifecycle ' + key, files_change=lambda f, k=key: update_json(
        f, 'hidden-recovery.json', lambda d: d['helper_intervals'][0].pop(k, None)))
for key in ('minimized_after_command', 'minimized_before_teardown'):
    publisher_case('missing minimized proof ' + key, files_change=lambda f, k=key: update_json(
        f, 'hidden-recovery.json', lambda d: d.update({k: False})))
publisher_case('main Process access', files_change=lambda f: update_json(
    f, 'hidden-recovery.json', lambda d: d.update(poison_messages=['Process accessed'])))
publisher_case('unexpected actual COM claim', files_change=lambda f: update_json(f, 'excel.json', lambda d: d.update(excel_com_tested=True)))
publisher_case('unexpected COM failure reason', files_change=lambda f: update_json(f, 'excel.json', lambda d: d.update(reason='other fault')))
publisher_case('incorrect package verifier digest', options={'verified_digest': '0' * 64})
publisher_case('existing immutable release', options={'existing_release': True})
publisher_case('existing immutable tag', options={'existing_tag': True})
publisher_case('changed tag prevents publication', options={'changed_tag': True})
publisher_case('bad asset digest prevents publication', options={'bad_asset': True})
for name in REQUIRED:
    publisher_case('missing critical ' + name, files_change=lambda f, n=name: f.update({'tests.txt': f['tests.txt'].replace(n.encode(), ('missing_' + n).encode())}))
special = b'Exercise frozen-row bookkeeping, not Excel formulas or equipment behavior.'
for label, changed in [('missing description', b''), ('changed description', b'other description.'),
                       ('skipped status', special + b' ... skipped'), ('inserted third line', special + b'\nextra')]:
    publisher_case('strict two-line case ' + label, files_change=lambda f, c=changed: f.update({'tests.txt': f['tests.txt'].replace(special, c)}))

report = dict(candidate_commit=COMMIT, candidate_zip_sha256=DIGEST,
              publisher_sha256=PUBLISHER_DIGEST, windows_run_id=RUN_ID,
              evidence_sha256={p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                               for p in EVIDENCE.iterdir() if p.is_file()},
              actual_package_verify_passed=True, network_calls_mocked=True,
              independent_review_gate_mocked=True, actual_publication_performed=False,
              passed=True, tests=len(results), cases=results)
output = Path(__file__).with_name('mock-publisher-results.json')
output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
print(json.dumps({'passed': True, 'tests': len(results), 'report': str(output)}))
