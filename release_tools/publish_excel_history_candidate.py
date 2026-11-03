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
VERSION = '2026.11.03-excel-wait.2'
CANDIDATE_COMMIT = '4e93d03b70b0c1abe782b9da8f4e614f6e01ccd2'
RUN_ID = 41585738069


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
    if args.candidate_commit != CANDIDATE_COMMIT:
        raise ValueError('Candidate is not the frozen reviewed source')
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
            review.get('publisher_sha256') == hashlib.sha256(Path(__file__).read_bytes()).hexdigest() and
            review.get('workflow_run_id') == RUN_ID and
            review.get('evaluation_distribution_allowed') is True and
            review.get('independent_post_review_complete') is True and
            review.get('equipment_application_approved') is False):
        raise ValueError('Completed independent evaluation-delivery review is required')
    verifier = Path(__file__).with_name('verify_package.py')
    if hashlib.sha256(verifier.read_bytes()).digest() != hashlib.sha256(
            (args.source_root / 'release_tools/verify_package.py').read_bytes()).digest():
        raise ValueError('Package verifier differs from the pinned source')
    if candidate['source_commit'] != args.candidate_commit or readiness['source_commit'] != args.candidate_commit:
        raise ValueError('Candidate/evidence commit mismatch')
    if candidate['version'] != VERSION or candidate['zip_sha256'] != args.expected_zip_sha256:
        raise ValueError('Candidate version/digest mismatch')
    run = gh_json(f'repos/{REPOSITORY}/actions/runs/{RUN_ID}')
    if (run.get('head_sha') != args.candidate_commit or run.get('status') != 'completed' or
            run.get('conclusion') != 'failure' or run.get('path') != '.github/workflows/validate-slice.yml'):
        raise ValueError('Unexpected candidate workflow provenance or result')
    # Compare every consumed evidence file to the exact artifact downloaded from
    # the pinned workflow run, rather than trusting locally assembled evidence.
    evidence_names = ('candidate.json', 'readiness.json', 'bridge.json', 'csv-guard.json',
                      'discovery.json', 'slice-performance.json', 'settings-recovery.json',
                      'background.json', 'excel.json', 'tests.txt', f'IRFeedback-{VERSION}.zip')
    with tempfile.TemporaryDirectory(prefix='excel-candidate-evidence-') as temporary:
        subprocess.run(['gh', 'run', 'download', str(RUN_ID), '--repo', REPOSITORY,
                        '--name', f'slice-candidate-{args.candidate_commit}', '--dir', temporary], check=True)
        for name in evidence_names:
            expected = hashlib.sha256((Path(temporary) / name).read_bytes()).digest()
            if hashlib.sha256((args.evidence / name).read_bytes()).digest() != expected:
                raise ValueError('Evidence differs from pinned workflow artifact: ' + name)
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
    required_tests = (
        'test_blocked_excel_does_not_block_csv_or_save', 'test_blocked_initialization_continues_polling',
        'test_blocked_reset_continues_polling', 'test_context_changes_invalidate_pending_result',
        'test_hold_logged_without_terminating_controller', 'test_image_handoff_discards_pending_features_and_waits_for_new_layer',
        'test_target_aba_during_process_invalidates_result_and_requires_fresh_row',
        'test_two_heaters_finish_before_save_and_report_partial_ambiguous_save',
        'test_watchdog_discards_late_result', 'test_recovers_same_session_using_same_prepared_workbook_and_fresh_row',
        'test_repeated_fault_backoff_is_not_reset_by_changes', 'test_unknown_retirement_keeps_polling_and_prevents_restart',
        'test_disconnect_in_backoff_prevents_restart', 'test_two_heaters_recovered_before_save',
        'test_fence_blocks_new_group_until_previous_verified', 'test_cancel_after_claim_before_thread_start_releases_fence',
        'test_snapshot_survives_source_replacement_and_deletion', 'test_known_excel_exit_is_waited_before_handle_close',
        'test_unknown_ownership_and_live_reader_block_replacement', 'test_starting_without_pid_stays_unknown',
        'test_stale_first_job_remains_pinned_after_outage', 'test_session_resumes_saving_after_rebind',
        'test_ambiguous_save_latches_and_rebind_preserves_identity', 'test_fault_registered_after_latch_delivers_original_once',
        'test_faults_stay_monitor_only_and_preserve_shared_settings', 'test_stop_during_backoff_wakes_without_another_attempt',
        'test_invalid_then_fresh_higher_layer', 'test_invalid_is_exposed_before_later_row_in_same_read',
        'test_wait_and_automatic_recovery_keep_connection_then_next_snapshot_reaches_chart',
        'test_observer_recovery_preserves_chart_and_does_not_archive_until_stop',
        'test_calculation_hold_event_is_logged_without_finishing_or_clearing_records',
        'test_partial_save_is_preserved_in_archive_csv_and_plot_input',
        'test_same_key_save_reaches_archive_csv_and_both_png_inputs',
        'test_owned_process_handle_is_terminated_and_verified',
        'test_invalid_pid_never_opens_process_and_ownership_stays_unknown',
        'test_non_ir_prepare_timeout_is_recoverable_when_helper_retires',
        'test_barrier_is_logged_before_queue_even_during_close',
        'test_uncertain_save_records_original_error_even_when_stop_is_set',
        'test_expired_accepted_rows_preserve_consecutive_history_and_third_save',
        'test_expiry_while_calculation_pending_discards_result_and_resets',
        'test_unknown_invalid_and_readiness_loss_reset_accepted_history',
        'test_synthetic_history_only_300_layers_expire_between_rows',
        'test_real_reader_expiry_preserves_workbook_history',
        'test_expired_known_row_marker_does_not_authorize_save',
        'test_expired_marker_requires_known_complete_valid_past_row',
        'test_matching_expired_idle_row_preserves_history_and_next_layer',
        'test_expired_history_requires_explicit_permission_and_exact_proof',
        'test_pending_and_unconsumed_results_are_never_kept_on_expiration',
        'test_image_change_resets_even_with_exact_expired_identity')
    for name in required_tests:
        description = (r"\nExercise frozen-row bookkeeping, not Excel formulas or equipment behavior\."
                       if name == 'test_synthetic_history_only_300_layers_expire_between_rows' else '')
        passed_line = re.findall(r"(?m)^" + re.escape(name) + r" \([^\n]+\)"
                                + description + r" \.\.\. ok\s*$", test_text)
        if len(passed_line) != 1:
            raise ValueError("Missing passed Excel wait regression: " + name)
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
    body = ('이전 평가판에서는 정상 기록이 약 5초 뒤 신선도 기한을 지나면 이미 수락한 Excel 계산 이력까지 초기화될 수 있었습니다. '
            '이번 평가판은 동일한 기록과 기존 안전 조건이 확인된 대기 중에는 수락한 이력을 보존합니다. '
            '기한이 지난 기록으로 새 계산이나 Save를 하지 않으며, 유효한 새 층을 기다립니다. '
            '기존처럼 전용 Excel 통합 문서를 열어 둔 상태에서 재사용하며, 별도의 수식 전용 계산 엔진으로 바꾸지 않았습니다. '
            '사용자 로그에는 일반 층 계산 약 0.26~0.35초와 이력 초기화 약 8초가 기록되어 있으나 이번 변경의 성능 개선 측정치는 아닙니다.\n\n'
            'Excel 초기화·이력 초기화·계산을 CSV 수신과 분리한 평가판입니다. '
            'Excel 응답 지연이나 통신 오류에는 IR 자동 적용을 일시정지하고 연결·CSV·그래프를 유지합니다. '
            '이전 Excel 프로세스와 리소스의 종료가 확인되면 검증된 원본 통합 문서 사본으로 자동 복구를 시도합니다. '
            '종료를 확인할 수 없으면 새 Excel을 시작하지 않고 대기합니다. 복구 후에는 새 층을 기다리며 늦은 결과는 적용하지 않습니다.\n\n'
            '일시적인 UI·CSV 읽기 장애와 센서의 잘못된 값은 관측을 이어가며 복구를 시도합니다. '
            '복구 중에는 IR 적용을 보류하고 유효한 기준 기록과 새로운 층을 확인합니다. '
            '처음 확인한 작업 식별을 유지하며 다른 작업에 자동 적용하지 않습니다. '
            'Save 결과가 불확실하면 재시도하지 않고 관측 전용 보류를 유지합니다. '
            '이미 성공한 일부 히터 Save와 그래프 기록은 보존합니다.\n\n'
            '초기화·이력 초기화·층 계산의 경과 시간, 단계와 프로세스 상태를 기록합니다. '
            '기존 오류만으로 실제 사용자 PC의 지연 원인을 단정하지 않았습니다. '
            '현재 층·작업·목표·설정·이미지·실제 IR·상한 및 Save 보호를 확인합니다. '
            '기존 Excel 수식·최대 IR 출력·5%p 제한·수동 이미지 ZIP/전체 선택·BL13과 CSV/두 PNG 저장은 유지합니다.\n\n'
            '기존 외부 모듈을 닫고 ZIP 전체를 새 폴더에 해제한 뒤 IRFeedback.exe를 실행하세요. '
            'JSON·DLL·runtime 폴더를 함께 유지합니다. 기존 알고리즘 XLSX 두 종류는 노션 첨부 또는 기존 파일을 '
            'Excel 수식 파일 → 찾아보기로 직접 선택합니다. Excel 데스크톱이 필요합니다.\n\n'
            f'Windows 프로그램 시험 {test_count}개와 기존 백그라운드 합성 시험 8개를 통과했습니다. '
            '새 자동 복구·지연·초기화·리셋·늦은 결과·목표/이미지/설정 변경·부분 Save 시험은 모의 계산기를 사용한 시험이며 '
            '실제 Microsoft Excel COM 지연을 재현한 결과가 아닙니다. '
            '백그라운드 전면 확인은 연결과 창 설정 이후부터 표본 측정합니다. '
            '실제 Excel COM·사용자 PC·BJT·센서·PLC·램프·잠금·절전·장시간 운전은 미검증입니다. '
            '최종 알고리즘 승인과 실제 장비 적용은 보류합니다.\n\n'
            f'검증 후보 소스: {args.candidate_commit}\nZIP SHA-256: {actual}')
    release = gh_json(f'repos/{REPOSITORY}/releases', data={
        'tag_name': VERSION, 'target_commitish': args.candidate_commit,
        'name': 'IRFeedback Excel 이력 보존 · 평가용', 'body': body,
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
