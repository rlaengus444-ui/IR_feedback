"""Offline strict-zero Process and helper lifecycle tamper tests."""
import unittest
from publish_m1_recovery_candidate import validate_hidden_process_evidence


def report():
    flags = {'helper_retired': True, 'active_helper_absent': True, 'unretired_helper_absent': True,
             'helper_fence_unlocked': True, 'shared_fence_verified': True}
    interval = {'begin_utc': '2026-11-09T11:59:00Z', 'end_utc': '2026-11-09T11:59:59Z'}
    return {'poison_messages': [], 'minimized_after_command': True, 'minimized_before_teardown': True,
            'checks': {'stayed_minimized': True},
            'helper_intervals': [dict(interval, transport='candidate', **flags),
                                 dict(interval, transport='old', child_exit_success=True, **flags)],
            'full_profiles': {'M1': {'process_not_probed': True, 'poison_messages': []}}}


class ProcessEvidenceTests(unittest.TestCase):
    def test_zero_process_and_retired_helpers(self):
        validate_hidden_process_evidence(report())

    def test_process_and_minimized_proofs_are_strict(self):
        for key in ('minimized_after_command', 'minimized_before_teardown'):
            for value in (False, None, 1):
                with self.subTest(key=key, value=value):
                    r = report(); r[key] = value
                    with self.assertRaises(ValueError): validate_hidden_process_evidence(r)
        for value in (['D'], ['3D'], ['114'], ['unclassified'], None):
            with self.subTest(messages=value):
                r = report(); r['poison_messages'] = value
                with self.assertRaises(ValueError): validate_hidden_process_evidence(r)
        r = report(); r['checks']['stayed_minimized'] = False
        with self.assertRaises(ValueError): validate_hidden_process_evidence(r)
        r = report(); r['full_profiles']['M1']['poison_messages'] = ['D']
        with self.assertRaises(ValueError): validate_hidden_process_evidence(r)

    def test_helper_retirement_and_historical_child_are_strict(self):
        for index in (0, 1):
            for key in ('helper_retired', 'active_helper_absent', 'unretired_helper_absent',
                        'helper_fence_unlocked', 'shared_fence_verified'):
                for value in (False, None, 1):
                    with self.subTest(index=index, key=key, value=value):
                        r = report(); r['helper_intervals'][index][key] = value
                        with self.assertRaises(ValueError): validate_hidden_process_evidence(r)
                r = report(); del r['helper_intervals'][index][key]
                with self.assertRaises(ValueError): validate_hidden_process_evidence(r)
        for value in (False, None, 1):
            r = report(); r['helper_intervals'][1]['child_exit_success'] = value
            with self.assertRaises(ValueError): validate_hidden_process_evidence(r)
        r = report(); del r['helper_intervals'][1]['child_exit_success']
        with self.assertRaises(ValueError): validate_hidden_process_evidence(r)
        r = report(); r['helper_intervals'][0]['transport'] = 'unknown'
        with self.assertRaises(ValueError): validate_hidden_process_evidence(r)
        r = report(); r['helper_intervals'].pop()
        with self.assertRaises(ValueError): validate_hidden_process_evidence(r)

    def test_bad_timestamps_rejected(self):
        for value in ('2026-11-09T11:59:00', 'bad', '2026-11-09T12:01:00Z'):
            r = report(); r['helper_intervals'][0]['begin_utc'] = value
            with self.assertRaises(ValueError): validate_hidden_process_evidence(r)


if __name__ == '__main__': unittest.main()
