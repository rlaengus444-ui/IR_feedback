"""Independent pre-edit guard tests; no UI interaction or original file access."""

from dataclasses import replace
import unittest

from sequence_field_audit import (SequenceFieldAuditError, assert_fields_match_persisted,
                                  field_bindings)
from sequence_persistence import SequencePersistenceSnapshot


def clean_fixture(machine="M1"):
    values = {}
    fields = {}
    for field, (scope, name, kind) in field_bindings(machine).items():
        if field == "textBoxHeatingOutput":
            fields[field] = "62.5"
            continue
        value_type, saved, displayed = ("BoolParameter", "True", "toggle:1") if kind == "toggle" else ("DoubleParameter", "5", "5.000")
        if field in ("textBoxPort1", "textBoxPort2", "textBoxPort3", "textBoxMultiPassCount"):
            value_type, saved, displayed = "IntParameter", "3", "3"
        values[f"{scope}.parameters.json\x1f{scope}\x1f{name}"] = (value_type, saved)
        fields[field] = displayed
    persisted = SequencePersistenceSnapshot("fixture/BJT.parameters.json", 62.5, "fixture-sha", 1, values)
    return fields, persisted


class SequenceFieldAuditTests(unittest.TestCase):
    def test_complete_clean_m1_and_s3_profiles_are_accepted(self):
        self.assertEqual(73, len(field_bindings("M1")))
        self.assertEqual(74, len(field_bindings("S3")))
        for machine in ("M1", "S3"):
            fields, saved = clean_fixture(machine)
            assert_fields_match_persisted(fields, saved, machine)
        self.assertEqual(("PrintingMotion", "X_CleaningPosition", "text"), field_bindings("M1")["textBoxCleanX"])
        self.assertEqual(("PrintingMotion", "X_PrintEffectiveMaxPosition", "text"), field_bindings("S3")["textBoxPrintMaxPos"])

    def test_every_unsaved_bound_field_is_rejected_before_admission(self):
        for machine in ("M1", "S3"):
            fields, saved = clean_fixture(machine)
            for field, (_, _, kind) in field_bindings(machine).items():
                dirty = dict(fields, **{field: "toggle:0" if kind == "toggle" else "99"})
                with self.subTest(machine=machine, field=field), self.assertRaisesRegex(SequenceFieldAuditError, field):
                    assert_fields_match_persisted(dirty, saved, machine)

    def test_missing_unknown_or_duplicate_mapping_rejects(self):
        fields, saved = clean_fixture()
        missing = dict(fields)
        missing.pop("textBoxCleanX")
        with self.assertRaisesRegex(SequenceFieldAuditError, "textBoxCleanX"):
            assert_fields_match_persisted(missing, saved, "M1")
        with self.assertRaisesRegex(SequenceFieldAuditError, "textBoxUnreviewed"):
            assert_fields_match_persisted(dict(fields, textBoxUnreviewed="5"), saved, "M1")
        duplicate = dict(saved.non_ir)
        scope, name, _ = field_bindings("M1")["textBoxCleanX"]
        duplicate[f"second.parameters.json\x1f{scope}\x1f{name}"] = ("DoubleParameter", "5")
        with self.assertRaisesRegex(SequenceFieldAuditError, "중복"):
            assert_fields_match_persisted(fields, replace(saved, non_ir=duplicate), "M1")

    def test_missing_owner_and_unsupported_type_fail_without_guessing(self):
        fields, saved = clean_fixture()
        key = next(key for key in saved.non_ir if key.endswith("\x1fX_CleaningPosition"))
        missing = dict(saved.non_ir)
        missing.pop(key)
        with self.assertRaisesRegex(SequenceFieldAuditError, "textBoxCleanX"):
            assert_fields_match_persisted(fields, replace(saved, non_ir=missing), "M1")
        unknown_type = dict(saved.non_ir)
        unknown_type[key] = ("PositionInchesParameter", "5")
        with self.assertRaisesRegex(SequenceFieldAuditError, "지원하지"):
            assert_fields_match_persisted(fields, replace(saved, non_ir=unknown_type), "M1")

    def test_units_locale_nonfinite_and_indeterminate_states_reject(self):
        fields, saved = clean_fixture()
        for field, value in (("textBoxCleanX", "5 mm"), ("textBoxCleanX", "5,000"),
                             ("textBoxCleanX", "NaN"), ("textBoxPort1", "3.0"),
                             ("checkBoxPass1Heater1", "toggle:2")):
            with self.subTest(field=field, value=value), self.assertRaises(SequenceFieldAuditError):
                assert_fields_match_persisted(dict(fields, **{field: value}), saved, "M1")

    def test_exact_known_unbound_controls_are_ignored_because_save_does_not_bind_them(self):
        fields, saved = clean_fixture()
        fields.update(checkBoxPrintNoPrinthead="toggle:1", textBoxPrintFinishMin="999",
                      textBoxSpreadingRollerRpm="n/a", checkBoxAdaptiveHeating="toggle:1")
        assert_fields_match_persisted(fields, saved, "M1")

    def test_float_canonical_roundoff_is_tolerated_but_real_edit_is_not(self):
        fields, saved = clean_fixture()
        assert_fields_match_persisted(dict(fields, textBoxHeatingOutput="62.5000000005"), saved, "M1")
        with self.assertRaises(SequenceFieldAuditError):
            assert_fields_match_persisted(dict(fields, textBoxHeatingOutput="62.500000002"), saved, "M1")


if __name__ == "__main__":
    unittest.main()
