import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import sequence_persistence as persistence


class SequencePersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.root = Path(self.temp.name)
        self.target = SimpleNamespace(machine="M1", image_path=str(self.root / "BinderJet.Unified.Operator.exe"))
        self.path = self.root / "Machines" / "M1" / "Configs" / "BJT.parameters.json"
        self.path.parent.mkdir(parents=True)

    def tearDown(self):
        self.temp.cleanup()

    def save(self, power="70", other="12", extra=()):
        entries = [{"Scope": "Printing", "Name": "IRHeatingOutputPct", "Type": "DoubleParameter", "Value": power},
                   {"Scope": "Printing", "Name": "LayerThickness", "Type": "DoubleParameter", "Value": other}]
        self.path.write_text(json.dumps(entries + list(extra)), encoding="utf-8")

    def test_capture_exact_process_image_path_and_saved_decimal(self):
        self.save("70.5")
        value = persistence.capture(self.target)
        self.assertEqual((str(self.path), 70.5), (value.path, value.power))
        self.assertEqual({"bjt.parameters.json\x1f__file__": True,
                          "bjt.parameters.json\x1fPrinting\x1fLayerThickness": ("DoubleParameter", "12")}, value.non_ir)
        self.assertEqual(64, len(value.sha256))

    def test_no_file_is_optional_and_does_not_create_any(self):
        self.assertIsNone(persistence.capture(self.target))
        self.assertFalse(self.path.exists())
        self.assertIsNone(persistence.verify_after(None, 70))

    def test_flat_legacy_layout_and_ambiguous_two_layouts(self):
        flat = self.root / "Configs" / "BJT.parameters.json"
        flat.parent.mkdir()
        self.save()
        flat.write_bytes(self.path.read_bytes())
        with self.assertRaises(persistence.PersistenceUnavailable):
            persistence.capture(self.target)
        self.path.unlink()
        self.assertEqual(str(flat), persistence.capture(self.target).path)

    def test_duplicate_or_wrong_scope_identity_rejected(self):
        self.save(extra=[{"Scope": "Printing", "Name": "IRHeatingOutputPct", "Value": "71"}])
        with self.assertRaises(persistence.PersistenceUnavailable):
            persistence.capture(self.target)
        self.path.write_text('[{"Scope":"Other","Name":"IRHeatingOutputPct","Value":"70"}]')
        with self.assertRaises(persistence.PersistenceUnavailable):
            persistence.capture(self.target)

    def test_invalid_and_out_of_range_values_rejected(self):
        for invalid in ("NaN", "Infinity", "-1", "100.1", "bad", True, None):
            with self.subTest(invalid=invalid):
                self.save(invalid)
                with self.assertRaises(persistence.PersistenceUnavailable):
                    persistence.capture(self.target)

    def test_bounded_read_and_malformed_json_rejected(self):
        self.path.write_bytes(b"x" * (persistence.MAXIMUM_OWNER_FILE_BYTES + 1))
        with self.assertRaises(persistence.PersistenceUnavailable):
            persistence.capture(self.target)
        self.path.write_text("{")
        with self.assertRaises(persistence.PersistenceUnavailable):
            persistence.capture(self.target)

    def test_read_detects_replacement_during_acquisition(self):
        self.save()
        original = persistence._read_shared
        def replacing(path):
            result = original(path)
            self.save("71")
            return result
        with patch.object(persistence, "_read_shared", replacing):
            with self.assertRaises(persistence.PersistenceUnavailable):
                persistence.capture(self.target)

    def test_earlier_owner_mutation_during_later_owner_read_is_detected(self):
        self.save()
        second = self.path.parent / 'Z.parameters.json'
        second.write_text('[]')
        original = persistence._read_shared
        changed = False
        def mutate(path):
            nonlocal changed
            data = original(path)
            if path == second and not changed:
                changed = True
                stat = self.path.stat()
                self.path.write_bytes(self.path.read_bytes().replace(b'"70"', b'"71"'))
                os.utime(self.path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            return data
        with patch.object(persistence, '_read_shared', mutate):
            with self.assertRaises(persistence.PersistenceUnavailable):
                persistence.capture(self.target)

    def test_verifies_ir_and_rejects_any_non_ir_change(self):
        self.save("70")
        before = persistence.capture(self.target)
        self.save("69")
        self.assertEqual(69, persistence.verify_after(before, 69, timeout=0).power)
        self.save("69", other="13")
        with self.assertRaises(persistence.PersistenceMismatch):
            persistence.verify_after(before, 69, timeout=0)

    def test_unchanged_wrong_power_times_out_without_any_writes(self):
        self.save("70")
        before = persistence.capture(self.target)
        raw = self.path.read_bytes()
        with self.assertRaises(persistence.PersistenceMismatch):
            persistence.verify_after(before, 69, timeout=0)
        self.assertEqual(raw, self.path.read_bytes())
        self.assertEqual(70, persistence.verify_after(before, 70, timeout=0).power)

    def test_motion_owner_mutation_is_detected_after_save(self):
        self.save("70")
        motion = self.path.parent / "MachineProfile.parameters.json"
        motion.write_text('[{"Scope":"PrintingMotion","Name":"X_PrintWindowMinPosition","Type":"DoubleParameter","Value":"12"}]')
        before = persistence.capture(self.target)
        self.save("69")
        motion.write_text('[{"Scope":"PrintingMotion","Name":"X_PrintWindowMinPosition","Type":"DoubleParameter","Value":"13"}]')
        with self.assertRaises(persistence.PersistenceMismatch):
            persistence.verify_after(before, 69, timeout=0)

    def test_owner_inventory_is_bounded_and_file_removal_is_detected(self):
        self.save()
        extra = self.path.parent / "Beckhoff.parameters.json"
        extra.write_text("[]")
        before = persistence.capture(self.target)
        extra.unlink()
        with self.assertRaises(persistence.PersistenceMismatch):
            persistence.verify_after(before, 70, timeout=0)
        for i in range(persistence.MAXIMUM_OWNER_FILES):
            (self.path.parent / f"test{i}.parameters.json").write_text("[]")
        with self.assertRaises(persistence.PersistenceUnavailable):
            persistence.capture(self.target)


if __name__ == "__main__":
    unittest.main()
