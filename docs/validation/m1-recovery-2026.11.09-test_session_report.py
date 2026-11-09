"""Offline schema and tamper tests using a real diagnostic, not release evidence."""
from copy import deepcopy
import json
from pathlib import Path
import unittest
from publish_m1_recovery_candidate import validate_hidden_session_case

REPORT = Path('/workspace/ir-m1-recovery-2026.11.09/native-diagnostic/hidden-session.json')
CASES = json.loads(REPORT.read_text(encoding='utf-8-sig'))['cases']


class SessionReportTests(unittest.TestCase):
    def test_actual_four_diagnostic_cases(self):
        self.assertEqual(4, len(CASES))
        for case in CASES:
            with self.subTest(machine=case['machine'], minimized=case['minimized']):
                validate_hidden_session_case(case)

    def test_different_ineligible_emission_does_not_invent_graph_row(self):
        case = deepcopy(CASES[0]); cap = case['readonly_pre_teardown']
        entry = {'phase': 'hidden-recovery', 'layer_index': 100, 'chart_eligible': False,
                 'observation_key_present': False, 'expired_observation_key': 'expired',
                 'ineligible_cause': 'Observation expired; production freshness guard removed its admitted key'}
        case['emitted_but_ineligible'].append(deepcopy(entry))
        cap['emitted_but_ineligible'].append(deepcopy(entry))
        cap['snapshots'].append(dict(entry, chart_record_accepted=False))
        validate_hidden_session_case(case)

    def test_missing_or_tampered_observation_proof_rejected(self):
        def changed(field, value):
            c = deepcopy(CASES[0]); c[field] = value; return c
        def mutate_snapshot(field, value):
            c = deepcopy(CASES[0]); layer = c['applied_layers'][0]
            for row in c['readonly_pre_teardown']['snapshots']:
                if row.get('layer_index') == layer - 1 and row.get('applied'):
                    row[field] = value
            return c
        mutations = [changed('eligible_emitted_layers', []), changed('applied_layers', [1, 2, 3, 4]),
                     changed('all_eligible_chart_observations_preserved', False),
                     changed('all_four_applied_fresh_layers_retained', False),
                     changed('emitted_but_ineligible', [{'ineligible_cause': 'invented'}]),
                     mutate_snapshot('observation_fresh', False), mutate_snapshot('applied', {}),
                     changed('chart_history_preserved', False)]
        for case in mutations:
            with self.subTest(case=mutations.index(case)):
                with self.assertRaises(ValueError): validate_hidden_session_case(case)

    def test_incomplete_teardown_evidence_rejected(self):
        for key, value in [('capture_error', 'error'), ('wrapper_outages', [1]), ('runner_calls', []),
                           ('native_calls', []), ('native_recoveries', []), ('history_layers', []),
                           ('foreground_maintained', False), ('saves', 0)]:
            with self.subTest(key=key):
                case = deepcopy(CASES[0]); case['readonly_pre_teardown'][key] = value
                with self.assertRaises(ValueError): validate_hidden_session_case(case)


if __name__ == '__main__': unittest.main()
