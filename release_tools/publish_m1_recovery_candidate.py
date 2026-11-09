"""Publish an already tested evaluation ZIP; never rebuild it or approve equipment use."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile

from verify_package import verify

REPOSITORY = 'rlaengus444-ui/IR_feedback'
VERSION = '2026.11.09-m1-recovery.1'
CANDIDATE_COMMIT = 'e97564fb998f31cce27328bc4e326b164869c649'
RUN_ID = 42601104496  # Full CI running; ZIP digest remains unpinned.
EXPECTED_ZIP_SHA256 = 'a98e2e4d183cbc2ec23a4a523f004e4bc6aa704d27640ea3aae5bee0f3bdbd4d'


def gh_json(*args, data=None):
    command = ['gh', 'api', *args]
    if data is not None:
        command += ['--input', '-']
    result = subprocess.run(command, input=None if data is None else json.dumps(data),
                            capture_output=True, text=True, encoding='utf-8', check=True)
    return json.loads(result.stdout)


def validate_hidden_process_evidence(report):
    """Require zero Process messages and proven helper retirement through teardown."""
    if (report.get('poison_messages') != [] or report.get('minimized_after_command') is not True or
        report.get('minimized_before_teardown') is not True or
        report.get('checks', {}).get('stayed_minimized') is not True):
        raise ValueError('Process access or minimized-state evidence failed')
    intervals = report.get('helper_intervals')
    if not isinstance(intervals, list) or not intervals:
        raise ValueError('Helper intervals missing')
    transports = set()
    for interval in intervals:
        if not isinstance(interval, dict):
            raise ValueError('Malformed helper interval')
        transport = interval.get('transport')
        if (transport not in ('candidate', 'old') or
            any(interval.get(key) is not True for key in ('helper_retired', 'active_helper_absent',
                'unretired_helper_absent', 'helper_fence_unlocked', 'shared_fence_verified')) or
            (transport == 'old' and interval.get('child_exit_success') is not True)):
            raise ValueError('Helper retirement not verified')
        transports.add(transport)
        try:
            start = datetime.fromisoformat(interval['begin_utc'].replace('Z', '+00:00'))
            finish = datetime.fromisoformat(interval['end_utc'].replace('Z', '+00:00'))
            if start.utcoffset() is None or finish.utcoffset() is None or finish < start:
                raise ValueError('Invalid helper interval')
        except (KeyError, TypeError, AttributeError, OverflowError) as error:
            raise ValueError('Invalid helper interval') from error
    if transports != {'candidate', 'old'}:
        raise ValueError('Historical and candidate helper evidence required')
    if any(profile.get('poison_messages') != [] or profile.get('process_not_probed') is not True
           for profile in report.get('full_profiles', {}).values()):
        raise ValueError('Full-profile Process access detected')


def validate_hidden_session_case(c):
    if (type(c.get('minimized')) is not bool or c.get('passed') is not True or
        any(c.get(k) is not True for k in ('worker_retired', 'foreground_maintained', 'non_ir_unchanged',
            'no_duplicate_or_old_row_saves', 'same_reader', 'same_native_adapter')) or
        c.get('process_table_accesses') != 0 or c.get('recovery_saves') != 0 or c.get('recovery_navigation') != 0 or
        c.get('powers') != [75, 70, 65, 60] or c.get('save_counts') != [1, 2, 3, 4]):
        raise ValueError('Hidden session behavioral evidence failed')
    capture = c.get('readonly_pre_teardown')
    if not isinstance(capture, dict) or 'capture_error' in capture:
        raise ValueError('Hidden session pre-teardown capture incomplete')
    calls = capture.get('native_calls', [])
    recoveries = capture.get('native_recoveries', [])
    successful_reads = [x for x in calls if x.get('phase') == 'hidden-recovery'
                        and x.get('operation') == 'recover_control' and x.get('success') is True]
    adoption = [x for x in recoveries if x.get('phase') == 'hidden-recovery'
                and type(x.get('before_epoch')) is int and type(x.get('after_epoch')) is int
                and x['after_epoch'] > x['before_epoch']]
    candidates = [x for x in recoveries if x.get('phase') == 'hidden-recovery'
                  and x.get('candidate_pending') is True and x.get('before_epoch') == x.get('after_epoch')]
    if (capture.get('fixture_running') is not True or capture.get('wrapper_outages') != [] or
        not isinstance(capture.get('runner_calls'), list) or len(capture['runner_calls']) != 1 or
        len(successful_reads) < 2 or len(adoption) != 1 or not candidates or
        capture.get('foreground_maintained') is not True or
        type(capture.get('foreground_samples')) is not int or capture['foreground_samples'] < 1 or
        capture.get('saves') != 4 or capture.get('navigation') != c.get('initial_navigation_count') or
        c.get('initial_navigation_count') != 1 or capture.get('non_ir_unchanged') is not True or
        c.get('same_observed_session') is not True or c.get('chart_history_preserved') is not True or
        type(capture.get('history_segment')) is not int or capture['history_segment'] < 1 or
        capture.get('history_layers') != c.get('final_history_layers') or
        not isinstance(capture.get('history_layers'), list) or not capture['history_layers']):
        raise ValueError('Hidden session pre-teardown evidence failed')
    snapshots = capture.get('snapshots')
    eligible = c.get('eligible_emitted_layers')
    ineligible = c.get('emitted_but_ineligible')
    applied = c.get('applied_layers')
    history = c.get('final_history_layers')
    if (not isinstance(snapshots, list) or not isinstance(eligible, list) or
        not isinstance(ineligible, list) or not isinstance(applied, list) or len(applied) != 4 or
        len(set(applied)) != 4 or not all(type(x) is int for x in applied) or
        history != eligible or capture.get('eligible_emitted_layers') != eligible or
        capture.get('emitted_but_ineligible') != ineligible or
        c.get('all_eligible_chart_observations_preserved') is not True or
        c.get('all_four_applied_fresh_layers_retained') is not True):
        raise ValueError('Hidden session observation summary incomplete')
    eligible_seen = sorted({x['layer_index'] + 1 for x in snapshots
                            if x.get('chart_eligible') is True and type(x.get('layer_index')) is int})
    ineligible_seen = [x for x in snapshots if x.get('chart_eligible') is False]
    if eligible != eligible_seen or len(ineligible_seen) != len(ineligible):
        raise ValueError('Hidden session observation accounting mismatch')
    for snapshot, entry in zip(ineligible_seen, ineligible):
        if (not isinstance(entry, dict) or not entry.get('ineligible_cause') or
            any(entry.get(k) != v for k, v in snapshot.items() if k != 'chart_record_accepted') or
            snapshot.get('chart_record_accepted') is True):
            raise ValueError('Hidden session ineligible observation proof mismatch')
    baseline = sorted({x['layer_index'] + 1 for x in snapshots if x.get('phase') == 'initial'
                       and x.get('chart_eligible') is True and type(x.get('layer_index')) is int
                       and x['layer_index'] + 1 <= c.get('initial_baseline_layer', -1)})
    if not baseline or history[:len(baseline)] != baseline or not set(applied).issubset(history):
        raise ValueError('Hidden session baseline or Save history lost')
    for layer, power in zip(applied, (75, 70, 65, 60)):
        if not any(x.get('layer_index') == layer - 1 and x.get('phase') == 'live-cap-apply'
                   and x.get('observation_fresh') is True and x.get('chart_eligible') is True
                   and x.get('applied', {}).get('1') == power for x in snapshots):
            raise ValueError('Hidden session fresh Save observation missing')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('evidence', type=Path)
    parser.add_argument('candidate_commit')
    parser.add_argument('expected_zip_sha256')
    parser.add_argument('review', type=Path)
    parser.add_argument('source_root', type=Path)
    args = parser.parse_args()
    if (RUN_ID is None or not re.fullmatch(r'[0-9a-f]{40}', CANDIDATE_COMMIT) or
            not re.fullmatch(r'[0-9a-f]{64}', EXPECTED_ZIP_SHA256)):
        raise ValueError('Final CI ZIP digest has not been pinned')
    if args.expected_zip_sha256 != EXPECTED_ZIP_SHA256:
        raise ValueError('ZIP digest is not the pinned reviewed candidate')
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
                      'background.json', 'hidden-recovery.json', 'hidden-session.json', 'excel.json', 'tests.txt', f'IRFeedback-{VERSION}.zip')
    with tempfile.TemporaryDirectory(prefix='excel-candidate-evidence-') as temporary:
        subprocess.run(['gh', 'run', 'download', str(RUN_ID), '--repo', REPOSITORY,
                        '--name', f'slice-candidate-{args.candidate_commit}', '--dir', temporary], check=True)
        for name in evidence_names:
            expected = hashlib.sha256((Path(temporary) / name).read_bytes()).digest()
            if hashlib.sha256((args.evidence / name).read_bytes()).digest() != expected:
                raise ValueError('Evidence differs from pinned workflow artifact: ' + name)
    required = ('build', 'tests', 'smoke', 'bridge', 'csv_guard', 'native_discovery', 'slice_perf', 'settings_recovery', 'background', 'hidden_recovery', 'hidden_session', 'reverify')
    if any(readiness['checks'].get(key) != 'success' for key in required):
        raise ValueError('A required Windows validation step failed')
    for name in ('bridge.json', 'csv-guard.json', 'discovery.json', 'slice-performance.json', 'settings-recovery.json', 'background.json', 'hidden-recovery.json', 'hidden-session.json'):
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
    hidden = read('hidden-recovery.json')
    for key, path in {'bridge_sha256': 'app/UiaBridge.dll', 'helper_sha256': 'app/native_helper.ps1'}.items():
        if hidden.get(key) != hashlib.sha256((args.source_root / path).read_bytes()).hexdigest():
            raise ValueError('Hidden recovery transport provenance mismatch: ' + key)
    checks = hidden.get('checks', {})
    expected_checks = {'initial_scope_proof', 'forged_root_scope_holds', 'focused_edit_holds', 'native_hide_style_evidence',
        'old_hidden_failure_reproduced', 'hidden_scope_recovery', 'winforms_hide_style_evidence',
        'winforms_hidden_scope_recovery', 'hidden_unproven_holds', 'global_anchor_invalid_rejected',
        'other_foreground_recovery', 'minimized_recovery', 'duplicate_id_holds',
        'unnamed_input_inventoried', 'unknown_id_in_inventory', 'paused_holds', 'reparented_scope_holds', 'heartbeat_responsive',
        'zero_save_or_navigation', 'process_not_probed', 'stayed_minimized', 'full_m1_s3_both_hide_modes_two_stable_audits'}
    expected_checks.update('explicit_hide_' + x + '_style_cleared' for x in ('inner', 'ir', 'save'))
    expected_checks.update('explicit_hidden_' + x + '_holds' for x in ('inner', 'ir', 'save'))
    if hidden.get('no_equipment_writes') is not True or any(checks.get(k) is not True for k in expected_checks):
        raise ValueError('Hidden recovery checks incomplete')
    profiles = hidden.get('full_profiles', {})
    if set(profiles) != {m + '_' + a for m in ('M1', 'S3') for a in ('hide_outer', 'hide_outer_managed')}:
        raise ValueError('Hidden recovery profiles differ')
    if any(p.get('passed') is not True or p.get('epochs') != [0, 1] or
           any(p.get(k) is not True for k in ('hidden_inner_held', 'unnamed_input_held', 'settings_unchanged',
                                               'zero_save_and_navigation', 'process_not_probed'))
           for p in profiles.values()):
        raise ValueError('Hidden recovery profile failed')
    validate_hidden_process_evidence(hidden)
    session = read('hidden-session.json')
    source_hashes = {'probe': 'release_tools/windows_hidden_session_probe.py', 'fixture': 'validation/CsvRouteFixture.cs',
                     **{n: 'app/' + n for n in ('native_helper.ps1', 'UiaBridge.dll', 'session.py', 'observed_session.py', 'resilient_adapter.py')}}
    if any(session.get('source_sha256', {}).get(k) != hashlib.sha256((args.source_root / path).read_bytes()).hexdigest()
           for k, path in source_hashes.items()):
        raise ValueError('Hidden session source provenance mismatch')
    cases = session.get('cases', [])
    identities = [(c.get('machine'), c.get('minimized')) for c in cases]
    if len(cases) != 4 or set(identities) != {(m, v) for m in ('M1', 'S3') for v in (False, True)}:
        raise ValueError('Hidden session case coverage differs')
    for c in cases:
        validate_hidden_session_case(c)
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
        'test_image_change_resets_even_with_exact_expired_identity',
        'test_one_race_retries_and_keeps_same_run',
        'test_retry_exhaustion_preserves_state_and_recovers',
        'test_incomplete_tail_never_publishes_and_then_completes',
        'test_backlog_progresses_without_publishing_partial',
        'test_rewrite_with_same_mtime_discards_provisional',
        'test_missing_anchor_and_missing_log_are_unavailable',
        'test_rotation_during_read_retries_and_publishes_new_job',
        'test_unavailable_log_does_not_clear_csv_state',
        'test_append_new_job_during_parse_is_retried',
        'test_new_latest_file_during_parse_is_selected',
        'test_same_mtime_prefix_rewrite_during_parse_is_detected',
        'test_korean_message_keeps_stable_reason',
        'test_parser_failure_does_not_commit_cursor',
        'test_transient_log_append_preserves_history_at_idle_and_preflight',
        'test_exhausted_preflight_does_not_save_or_replay_and_preserves_previous_save',
        'test_stable_new_job_rejects_old_decision_and_preserves_previous_save',
        'test_fault_transitions_are_logged_before_queue_even_during_close',
        'test_unknown_state_preserves_original_reason_and_logs_transition_once',
        'test_excel_reset_records_specific_reason_only_once',
        'test_reset_diagnostics_count_actual_resets_and_coalesce',
        'test_reset_diagnostics_capture_pending_and_unconsumed',
        'test_rejected_group_reset_is_not_counted',
        'test_hold_reason_identifies_actual_cause',
        'test_log_read_unavailable_preserves_specific_reason',
        'test_denied_rotation_preserves_state_until_retry',
        'test_lowered_during_real_preflight_denies_before_edit',
        'test_change_after_gate_does_not_repeat_or_cancel_admitted_save',
        'test_denied_created_helper_is_reaped_without_payload',
        'test_denied_unretired_helper_fences_without_ambiguous_write',
        'test_gate_runs_once_even_if_communication_polls',
        'test_forwarding_survives_rebind_and_nonapply_does_not_gate',
        'test_ramp_history_and_fractional_cap',
        'test_bounds_zero_and_smaller_step',
        'test_beta_saturation_survives_raising_or_lowering_cap',
        'test_cap_state_is_transactional_and_reset',
        'test_original_actual_bounds_remain_enforced',
        'test_pending_cap_aba_discards_result_and_resets',
        'test_unconsumed_cap_change_discards_completed_result',
        'test_idle_cap_change_keeps_accepted_history',
        'test_immediate_result_retains_cap_stamp_through_recursive_poll',
        'test_physical_ceiling_allows_later_increase_above_initial_user_cap',
        'test_lower_limit_ramps_on_distinct_fresh_layers_only',
        'test_increase_above_initial_limit_applies_on_fresh_observation',
        'test_fractional_actual_ramps_to_zero_without_overshoot',
        'test_idle_cap_change_preserves_accepted_excel_history',
        'test_json_minimum_rejection_and_custom_two_percent_step',
        'test_cap_aba_while_excel_busy_discards_old_result',
        'test_basic_controller_cap_aba_during_batch_resets_both_histories',
        'test_workbook_recovery_keeps_requested_limit',
        'test_change_between_heaters_keeps_first_save_and_blocks_second',
        'test_stale_queued_decision_is_hidden_but_confirmed_save_remains',
        'test_legacy_snapshot_without_decision_tag_is_not_filtered',
        'test_initial_and_changed_cap_remain_pending_during_old_calculation',
        'test_cap_change_at_activation_discards_decision_and_waits_fresh',
        'test_bounds_and_status',
        'test_aba_and_admission',
        'test_no_lock_held_during_admission',
        'test_ramp_and_validation',
        'test_cap_remains_editable_and_apply_only_requests_next_layer',
        'test_minimum_rejection_preserves_active_cap_and_saved_preference',
        'test_stale_ui_ack_keeps_pending_request_and_confirmed_save_marker',
        'test_close_saves_last_requested_limit_not_an_unapplied_draft',
        'test_storage_failure_does_not_rollback_accepted_cap_request',
        'test_invalid_cap_never_starts_a_worker_and_wait_mode_keeps_pumping',
        'test_corrupt_preference_requires_explicit_input_and_close_stores_valid_cap',
        'test_invalid_control_bounds_remain_unbound_in_monitor_fallback',
        'test_bound_limit_survives_monitor_fallback',
        'test_above_user_limit_ramps_on_each_fresh_layer',
        'test_above_json_hard_bound_holds_until_baseline_and_fresh_row',
        'test_json_minimum_above_user_cap_is_rejected',
        'test_builtin_history_uses_effective_cap_for_saturation',
        'test_lower_json_maximum_is_effective_controller_bound',
        'test_adversarial_controller_cannot_save_outside_limit_or_step',
        'test_reconnect_prepares_once_then_baseline_and_fresh_layer',
        'test_repeated_validated_failures_keep_one_shot',
        'test_revocation_cannot_be_recreated_on_same_object',
        'test_factory_and_disconnect_failures_cannot_resurrect_grant',
        'test_monitor_prepare_validate_error_does_not_grant',
        'test_each_original_identity_component_is_bound',
        'test_discovery_failure_retries_only_when_retired_and_read_only',
        'test_sticky_fault_revokes_unused_grant',
        'test_unsafe_or_wrong_phase_never_grants',
        'test_rebind_and_ambiguous_identity_revoke_grant',
        'test_monitor_preview_and_stop_do_not_prepare',
        'test_grant_consumed_before_failed_preparation',
        'test_validate_timeout_retains_cap_and_requires_new_observations',
        'test_scope_proof_is_carried_and_change_requires_two_new_checks',
        'test_held_cached_and_same_layer_rows_do_not_save',
        'test_names_use_machine_timestamp_and_atomic_numbered_collisions')
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
    body = ('M1의 가려진 Sequence 설정 복구를 개선한 평가판입니다. 지원 장비는 M1·S3이며 M2는 지원하지 않습니다. '
            '검증된 영역과 전체 설정을 재확인하고 기준 관찰과 새 층을 기다립니다. '
            '복구 중 탭 이동이나 Save를 하지 않으며 미저장 편집과 필수 항목 숨김은 대기합니다.\n\n'
            '자동 저장 파일은 장비와 저장 시각을 사용하며 CSV와 _온도.png, _IR.png를 생성합니다. '
            '충돌 시 번호를 붙입니다. 압축을 푼 IRFeedback.exe 옆 records 폴더에 저장하며 실패 시 %LOCALAPPDATA%/IRFeedback/records를 사용합니다.\n\n'
            f'Windows 프로그램 시험 {test_count}개와 합성 백그라운드·숨김 복구·세션 시험을 확인했습니다. '
            '합성 복구 시험은 최소화 창의 복원을 시험하지 않았고, 실제로 빈 UIA AutomationId도 미검증입니다. '
            '실제 Excel COM·BJT·센서·PLC·램프·잠금 화면·절전과 장시간 운전은 미검증입니다. '
            '최종 알고리즘 승인과 실제 장비 적용은 보류합니다.\n\n'
            f'검증 후보 소스: {args.candidate_commit}\nZIP SHA-256: {actual}')
    release = gh_json(f'repos/{REPOSITORY}/releases', data={
        'tag_name': VERSION, 'target_commitish': args.candidate_commit,
        'name': 'IRFeedback M1 화면 복구 · 평가용', 'body': body,
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
