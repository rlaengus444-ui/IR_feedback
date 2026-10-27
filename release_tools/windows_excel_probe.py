"""Exercise the real Excel COM bridge on an isolated generated workbook."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import traceback

result_path = Path(sys.argv[1])
result = {'excel_com_tested': False, 'release_ready': False}
try:
    # Registry availability is only discovery; WorkbookController must launch Excel itself.
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r'Excel.Application\CLSID'):
            pass
    except FileNotFoundError:
        result['reason'] = 'Excel COM is not registered'
    else:
        from openpyxl import Workbook
        from excel_workbook import WorkbookController, WorkbookSpec
        from slice_data import FEATURE_NAMES
        with tempfile.TemporaryDirectory(prefix='ir-excel-probe-') as directory:
            path = Path(directory) / 'fixture.xlsx'
            book = Workbook()
            sheet = book.active
            sheet.title = 'Algorithm'
            sheet['B5'] = 60
            # E:P represent current and previous image inputs. Row 14 reads a
            # prior frozen formula plus the current and previous coverage cells.
            sheet['D13'] = '=C13+IF(E13=1,F13/100,0)+(B5-60)/100'
            sheet['D14'] = '=D13+IF(I14=1,J14/100,0)+IF(ISBLANK(F14),1,0)'
            sheet['D15'] = '=C15+IF(E15=1,F15/100,0)'
            book.save(path)
            original = hashlib.sha256(path.read_bytes()).hexdigest()
            spec = WorkbookSpec(str(path), 'Algorithm', 'B13', 'C13', 'D13', 'B5', 'A13', 'E13')
            features = dict.fromkeys(FEATURE_NAMES)
            features.update(image_valid=1, center_coverage_pct=20, center_intensity_pct=30, whole_coverage_pct=40,
                            previous_image_valid=0, previous_powder_layers=0, radius_mm=1, dpi_x=400, dpi_y=400)
            controller = WorkbookController(spec, 60, timeout=30)
            try:
                def raw(layer, values):
                    return controller.process(layer, 60, 50, slice_features=values).diagnostics['excel_raw']
                first = raw(1, features)
                missing = dict(features, image_valid=0, center_coverage_pct=None, center_intensity_pct=None,
                               whole_coverage_pct=None, previous_image_valid=1, previous_center_coverage_pct=30,
                               previous_center_intensity_pct=40, previous_whole_coverage_pct=50)
                controller.set_target_temperature(80)
                second = raw(2, missing)
                if abs(first - 50.2) > 1e-7 or abs(second - 51.5) > 1e-7:
                    raise AssertionError(f'Unexpected COM results: {first}, {second}')
                controller.reset()
                # Reset restores formulas while retaining the newly selected target.
                if abs(raw(1, features) - 50.4) > 1e-7:
                    raise AssertionError('Reset did not restore workbook with the active target')
            finally:
                controller.close()
            reopened = WorkbookController(spec, 60, timeout=30)
            try:
                again = reopened.process(1, 60, 50, slice_features=features).diagnostics['excel_raw']
                if abs(again - first) > 1e-7:
                    raise AssertionError('Reopened workbook did not restore formulas')
            finally:
                reopened.close()
            if hashlib.sha256(path.read_bytes()).hexdigest() != original:
                raise AssertionError('Source workbook changed')
            result.update(excel_com_tested=True, release_ready=True, first=first, second=second)
except Exception:
    result['error'] = traceback.format_exc()
finally:
    result_path.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
print(json.dumps(result, indent=2))
if not result['release_ready']:
    sys.exit(1)
