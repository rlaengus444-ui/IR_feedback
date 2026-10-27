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
VERSION = '2026.10.28-s3.1'


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
    args = parser.parse_args()
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
    required = ('build', 'tests', 'smoke', 'bridge', 'csv_guard', 'native_discovery', 'reverify')
    if any(readiness['checks'].get(key) != 'success' for key in required):
        raise ValueError('A required Windows validation step failed')
    for name in ('bridge.json', 'csv-guard.json', 'discovery.json'):
        if read(name).get('passed') is not True:
            raise ValueError('Failed native validation: '+name)
    if readiness.get('device_application_approved') is not False or readiness.get('release_ready') is not False:
        raise ValueError('Evaluation status must not masquerade as equipment/stable approval')
    excel = read('excel.json')
    if excel.get('excel_com_tested') is not False or excel.get('reason') != 'Excel COM is not registered':
        raise ValueError('Unexpected Excel status; review before distribution')
    archive = args.evidence / (f'IRFeedback-{VERSION}.zip')
    actual = verify(archive, VERSION)
    if actual != args.expected_zip_sha256:
        raise ValueError('Verified ZIP digest mismatch')
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
    body = ('S3 연결 준비 중 900-child 오류를 수정한 평가용 배포입니다. '
            '전체 HWND 방문 수와 보이는 후보 수를 분리하며, 중복·소유권·출력 적용 보호는 유지합니다. '
            'slice.2의 내장 알고리즘과 별도 계산시트 두 종류는 변경하지 않습니다.\n\n'
            'Windows 프로그램 159개 시험, S3 모의 창의 실제 enter_sequence/metadata/prepare_control, '
            '기존 오류 재현 및 중복·900/901 경계·변경된 핸들 거부를 검증했습니다. '
            'DLL 다운로드 표시·변조·누락 검사와 전체 파일 해시를 확인했습니다.\n\n'
            '실제 Excel COM 및 S3 장비 시험은 미실행입니다. '
            '제어 알고리즘 최종 승인과 실제 장비 적용은 계속 보류합니다.\n\n'
            f'검증 후보 소스: {args.candidate_commit}\nZIP SHA-256: {actual}')
    release = gh_json(f'repos/{REPOSITORY}/releases', data={
        'tag_name': VERSION, 'target_commitish': args.candidate_commit,
        'name': 'IRFeedback S3 연결 오류 수정 · 평가용', 'body': body,
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
