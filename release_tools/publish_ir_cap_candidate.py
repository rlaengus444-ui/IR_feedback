"""Publish an already tested evaluation ZIP; never rebuild it or approve equipment use."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile

from verify_package import verify

REPOSITORY = 'rlaengus444-ui/IR_feedback'
VERSION = '2026.11.03-ir-cap.1'


def gh_json(*args, data=None):
    command = ['gh', 'api', *args]
    if data is not None:
        command += ['--input', '-']
    result = subprocess.run(command, input=None if data is None else json.dumps(data),
                            capture_output=True, text=True, encoding='utf-8', check=True)
    return json.loads(result.stdout)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('evidence', type=Path)
    parser.add_argument('candidate_commit')
    parser.add_argument('expected_zip_sha256')
    parser.add_argument('review', type=Path)
    parser.add_argument('source_root', type=Path)
    args = parser.parse_args()
    source_commit = subprocess.check_output(['git', '-C', str(args.source_root), 'rev-parse', 'HEAD'], text=True).strip()
    if source_commit != args.candidate_commit:
        raise ValueError('Reference source checkout is not the reviewed candidate')
    if subprocess.check_output(['git', '-C', str(args.source_root), 'status', '--porcelain'], text=True).strip():
        raise ValueError('Reference source checkout has modified or untracked files')
    for value, length in ((args.candidate_commit, 40), (args.expected_zip_sha256, 64)):
        if not re.fullmatch('[0-9a-f]{'+str(length)+'}', value):
            raise ValueError('A full pinned commit and ZIP digest are required')
    read = lambda name: json.loads((args.evidence / name).read_text(encoding='utf-8-sig'))
    candidate, readiness = read('candidate.json'), read('readiness.json')
    review = json.loads(args.review.read_text(encoding='utf-8'))
    if not (review.get('candidate_commit') == args.candidate_commit and
            review.get('candidate_zip_sha256') == args.expected_zip_sha256 and
            review.get('evaluation_distribution_allowed') is True and
            review.get('independent_post_review_complete') is True and
            review.get('equipment_application_approved') is False):
        raise ValueError('Completed independent evaluation-delivery review is required')
    if candidate['source_commit'] != args.candidate_commit or readiness['source_commit'] != args.candidate_commit:
        raise ValueError('Candidate/evidence commit mismatch')
    if candidate['version'] != VERSION or candidate['zip_sha256'] != args.expected_zip_sha256:
        raise ValueError('Candidate version/digest mismatch')
    required = ('build', 'tests', 'smoke', 'bridge', 'csv_guard', 'native_discovery', 'slice_perf', 'settings_recovery', 'background', 'reverify')
    if any(readiness['checks'].get(key) != 'success' for key in required):
        raise ValueError('A required Windows validation step failed')
    for name in ('bridge.json', 'csv-guard.json', 'discovery.json', 'slice-performance.json', 'settings-recovery.json', 'background.json'):
        if read(name).get('passed') is not True:
            raise ValueError('Failed native validation: '+name)
    background = read('background.json')
    provenance = {'harness_sha256': args.source_root / 'validation/csv_route_probe.py',
                  'bridge_sha256': args.source_root / 'app/UiaBridge.dll',
                  'helper_sha256': args.source_root / 'app/native_helper.ps1'}
    if any(background.get(key) != hashlib.sha256(path.read_bytes()).hexdigest()
           for key, path in provenance.items()):
        raise ValueError('Background native transport/harness provenance mismatch')
    checks = background.get('checks', {})
    if any(checks.get(key) is not True for key in (
            'eight_fixture_cases_passed', 'four_background_native_saves',
            'four_dirty_or_paused_hold_without_save')):
        raise ValueError('Background case or Save/hold checks failed')
    cases = background.get('cases')
    expected_cases = {(machine, minimized, case) for machine in ('M1', 'S3')
                      for minimized, case in ((False, 'normal'), (True, 'normal'),
                                              (False, 'dirty'), (False, 'paused'))}
    if not isinstance(cases, list) or len(cases) != 8:
        raise ValueError('Background report must contain eight cases')
    seen = set()
    for case in cases:
        if not isinstance(case, dict) or type(case.get('minimized')) is not bool:
            raise ValueError('Invalid background case')
        identity = (case.get('machine'), case['minimized'], case.get('case'))
        if (identity not in expected_cases or identity in seen or case.get('passed') is not True or
                case.get('different_process_foreground') is not True or
                case.get('foreground_at_end') is not True or
                type(case.get('saveCount')) is not int or
                case['saveCount'] != (1 if identity[2] == 'normal' else 0) or
                type(case.get('foreground_samples')) is not int or case['foreground_samples'] < 1 or
                case.get('processTableAccesses') != 0 or case.get('nonIrUnchanged') is not True or
                (identity[2] == 'dirty' and case.get('connected_dirty_hold_observed') is not True)):
            raise ValueError('Background case identity, foreground or Save/hold mismatch')
        seen.add(identity)
    if seen != expected_cases:
        raise ValueError('Background case coverage mismatch')
    if (readiness.get('device_application_approved') is not False or
            readiness.get('release_ready') is not False or
            readiness.get('automated_checks_passed') is not False or
            readiness.get('final_sol_review') != 'pending' or
            readiness['checks'].get('excel') != 'failure'):
        raise ValueError('Evaluation status must not masquerade as equipment/stable approval')
    excel = read('excel.json')
    if excel.get('excel_com_tested') is not False or excel.get('reason') != 'Excel COM is not registered':
        raise ValueError('Unexpected Excel status; review before distribution')
    archive = args.evidence / (f'IRFeedback-{VERSION}.zip')
    actual = verify(archive, VERSION, root=args.source_root)
    if actual != args.expected_zip_sha256:
        raise ValueError('Verified ZIP digest mismatch')
    test_text = (args.evidence / 'tests.txt').read_text(encoding='utf-8-sig')
    matched = re.search(r'Ran (\d+) tests', test_text)
    if matched is None or not re.search(r'\nOK\s*$', test_text):
        raise ValueError('Actual Windows application test summary is missing')
    test_count = int(matched.group(1))
    # The version is immutable: do not replace an existing release or its assets.
    releases = gh_json(f'repos/{REPOSITORY}/releases?per_page=100')
    if any(release['tag_name'] == VERSION for release in releases):
        raise ValueError('Release already exists; refusing to overwrite it')
    tag_ref = 'refs/tags/'+VERSION
    refs = gh_json(f'repos/{REPOSITORY}/git/matching-refs/tags/{VERSION}')
    if any(ref['ref'] == tag_ref for ref in refs):
        raise ValueError('Tag already exists; refusing to reuse it')
    created_tag = gh_json(f'repos/{REPOSITORY}/git/refs', data={
        'ref': tag_ref, 'sha': args.candidate_commit})
    if created_tag.get('ref') != tag_ref or created_tag['object']['sha'] != args.candidate_commit:
        raise ValueError('New version tag does not identify the reviewed candidate')

    def assert_tag():
        ref = gh_json(f'repos/{REPOSITORY}/git/ref/tags/{VERSION}')
        if ref.get('ref') != tag_ref or ref['object'].get('type') != 'commit' or ref['object']['sha'] != args.candidate_commit:
            raise ValueError('Version tag changed from the reviewed candidate')

    assert_tag()
    body = ('연결 전에 최대 IR 출력(0~100%, 기본 100%)을 지정하는 평가판입니다. '
            '설정은 연결 중 고정되고 %LOCALAPPDATA%/IRFeedback/ir-output-limit.json에 저장됩니다. '
            '기본 알고리즘은 JSON 최대값과 사용자 상한 중 작은 값을 사용하고 최소값이 이를 넘으면 거부합니다. '
            '기본·Excel 결과에 상한과 1회 최대 5%p 증감 제한을 적용하며 현재 설정이 상한을 넘으면 Save를 대기합니다. '
            '대기 중 CSV와 그래프는 유지합니다.\n\n'
            'Excel은 원시 수식 결과를 0~100%로 검증한 뒤 제한합니다. 수식과 확정된 이력은 유지하고 '
            '현재 IR 입력에는 실제 설정값을 사용합니다. 첫 탭 경로 자동 불러오기와 감시는 제거했습니다. '
            'ZIP 또는 이미지 다중 선택으로 수동 분석하며 기본 DPI는 400×400입니다. '
            '외부에서 제공받는 이미지 회복제어 및 파우더층 제어·분사층 출력 고정 XLSX 두 종류는 포함되지 않으며 '
            '기존 XLSX를 Excel 수식 파일 → 찾아보기로 직접 선택합니다. 이미지 시작셀 기본값은 BL13입니다.\n\n'
            '기존 외부 모듈을 닫고 ZIP 전체를 새 폴더에 압축 해제한 뒤 IRFeedback.exe를 실행하세요. '
            '설정 JSON·DLL·runtime 폴더를 함께 유지합니다. Excel 수식 모드에는 Excel 데스크톱이 필요합니다.\n\n'
            f'Windows 프로그램 시험 {test_count}개와 합성 창의 백그라운드 시험 8개를 통과했습니다. '
            'M1/S3의 일반·최소화 정상 사례에서 Save 4회, 미저장 편집·일시정지 사례에서 Save 없이 대기 4회를 확인했습니다. '
            '연결과 탭·최소화 설정을 마친 후 다른 프로세스를 한 번 전면에 띄워 이후 CSV 처리·저장 중 전면 유지를 표본 확인했습니다. '
            '초기 연결 준비·최소화 전환 중 전면 유지와 실제 BJT, Excel COM, 센서·PLC·램프, '
            '잠금 화면·절전·권한 불일치 및 장시간 운전은 미검증입니다. '
            '최종 알고리즘 승인과 실제 장비 적용은 보류합니다.\n\n'
            f'검증 후보 소스: {args.candidate_commit}\nZIP SHA-256: {actual}')
    release = gh_json(f'repos/{REPOSITORY}/releases', data={
        'tag_name': VERSION, 'target_commitish': args.candidate_commit,
        'name': 'IRFeedback IR 출력 상한 · 평가용', 'body': body,
        'draft': True, 'prerelease': True})
    def assert_release(value, draft):
        if value.get('tag_name') != VERSION or value.get('draft') is not draft or value.get('prerelease') is not True:
            raise ValueError('Release tag/draft/prerelease state differs')
    assert_release(release, True)
    # Preserve the original Actions checksum as evidence; publish a separate file.
    checksum_data = f'{actual}  {archive.name}\n'.encode('utf-8')
    expected = {archive.name: (actual, archive.stat().st_size),
                'SHA256.txt': (hashlib.sha256(checksum_data).hexdigest(), len(checksum_data))}
    with tempfile.TemporaryDirectory(prefix='s3-publish-') as temporary:
        checksum = Path(temporary) / 'SHA256.txt'
        checksum.write_bytes(checksum_data)
        subprocess.run(['gh', 'release', 'upload', VERSION, str(archive), str(checksum),
                        '--repo', REPOSITORY], check=True)

    def assert_assets():
        assets = gh_json(f'repos/{REPOSITORY}/releases/{release["id"]}/assets')
        if len(assets) != len(expected) or any(
            a['name'] not in expected or a.get('state') != 'uploaded' or
            a.get('digest') != 'sha256:'+expected[a['name']][0] or
            a.get('size') != expected[a['name']][1] for a in assets):
            raise ValueError('Uploaded asset state/size/digests differ')

    assert_assets()
    assert_tag()
    assert_release(gh_json(f'repos/{REPOSITORY}/releases/{release["id"]}'), True)
    gh_json(f'repos/{REPOSITORY}/releases/{release["id"]}', '-X', 'PATCH', data={
        'draft': False, 'prerelease': True, 'make_latest': 'false'})
    published = gh_json(f'repos/{REPOSITORY}/releases/{release["id"]}')
    assert_release(published, False)
    assert_tag()
    assert_assets()
    print(json.dumps({'release_url': published['html_url'], 'source_commit': args.candidate_commit,
                      'zip_sha256': actual, 'equipment_application_approved': False}, indent=2))


if __name__ == '__main__':
    main()
