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
VERSION = '2026.10.30-csv-wait.1'


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
    required = ('build', 'tests', 'smoke', 'bridge', 'csv_guard', 'native_discovery', 'slice_perf', 'settings_recovery', 'auto_slice', 'reverify')
    if any(readiness['checks'].get(key) != 'success' for key in required):
        raise ValueError('A required Windows validation step failed')
    for name in ('bridge.json', 'csv-guard.json', 'discovery.json', 'slice-performance.json', 'settings-recovery.json', 'auto-slice.json'):
        if read(name).get('passed') is not True:
            raise ValueError('Failed native validation: '+name)
    if readiness.get('device_application_approved') is not False or readiness.get('release_ready') is not False:
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
    body = ('Monitoring CSV 기록 중 또는 새 완료 행 수신이 IR 저장 전 검증과 겹칠 때 연결이 종료되던 문제를 수정한 평가판입니다. '
            '해당 계산을 취소하고 연결·CSV 조회·그래프를 유지하며, 기준 관찰 후 다른 새 층부터 계산을 재개합니다. '
            'IR 입력 또는 Save가 시작된 오류의 결과 불명확 중단·재시도 금지는 유지합니다.\n\n'
            '실제 제공 CSV 258행과 피드백 기록 216행을 대조했고, 실제 행을 사용한 전체/부분 기록 경합을 오프라인에서 재현했습니다. '
            '조회·기본 또는 Excel 계산·재검증/저장 요청 시간을 상태 기록에 남깁니다. '
            '강제 주입한 파일 변경 시험은 실제 Excel 계산시간 측정이 아닙니다.\n\n'
            '기존 이미지 자동/수동 불러오기, 중앙 반경·DPI·층 대응, UI 자동 적용, 최근 100층/전체 그래프, '
            '전체 CSV 저장과 연결 종료 시 CSV·온도·IR PNG 자동 저장은 유지합니다. 제어 수식과 기존 계산시트 두 종류는 변경하지 않았습니다.\n\n'
            '기존 외부 모듈을 닫고 ZIP 전체를 새 폴더에 압축 해제한 뒤 IRFeedback.exe를 실행하세요. '
            '설정 JSON·DLL·runtime 폴더를 함께 유지합니다. Excel 수식 모드에는 Excel 데스크톱이 필요하며, '
            '기존 계산시트를 직접 선택하고 이미지 시작셀 BL13을 사용합니다.\n\n'
            f'Windows 프로그램 시험 {test_count}개, 네이티브 CSV 가드 20개 조건과 DLL·CSV·S3 탐색·설정 복구·이미지/전체 패키지 검증을 통과했습니다. '
            '독립 Sol 코드 및 해당 배포 파일 검토를 완료했습니다(gpt-6.1-sol/max 요청). '
            '검증 환경에는 Excel COM이 없으며 실제 계산속도·BJT·S3 센서·PLC·램프·장시간 프린팅은 미검증입니다. '
            '최종 알고리즘 승인과 실제 장비 적용은 보류합니다.\n\n'
            f'검증 후보 소스: {args.candidate_commit}\nZIP SHA-256: {actual}')
    release = gh_json(f'repos/{REPOSITORY}/releases', data={
        'tag_name': VERSION, 'target_commitish': args.candidate_commit,
        'name': 'IRFeedback CSV 갱신 연결 유지 · 평가용', 'body': body,
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
