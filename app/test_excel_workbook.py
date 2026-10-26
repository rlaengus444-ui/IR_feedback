import io
import math
import queue
import threading
import unittest
import zipfile

from excel_workbook import WorkbookController, WorkbookSpec, inspect_workbook, _reject_cross_sheet_history


def workbook(*, recommendation='C13+B13', extra='', relationship='', layer='1', temp='65', actual='60', row14=''):
    book = '''<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Algorithm" sheetId="1" r:id="rId1"/></sheets></workbook>'''
    rels = '''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Target="worksheets/sheet1.xml" Type="worksheet"/>%s</Relationships>''' % relationship
    sheet = '''<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="5"><c r="B5"><v>65</v></c></row><row r="13"><c r="A13"><v>%s</v></c><c r="B13"><v>%s</v></c><c r="C13"><v>%s</v></c><c r="D13"><f>%s</f><v>60</v></c>%s</row><row r="14"><c r="D14"><f>C14+B14</f></c>%s</row></sheetData></worksheet>''' % (layer, temp, actual, recommendation, extra, row14)
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('xl/workbook.xml', book)
        z.writestr('xl/_rels/workbook.xml.rels', rels)
        z.writestr('xl/worksheets/sheet1.xml', sheet)
    return out.getvalue()


def spec(**changes):
    values = dict(path='example.xlsx', sheet='', temperature_cell='B13', actual_power_cell='C13', recommendation_cell='D13', target_cell='B5', layer_cell='A13')
    values.update(changes)
    return WorkbookSpec(**values)


def add_auxiliary_sheet(data, formula):
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as source, zipfile.ZipFile(output, 'w') as destination:
        for info in source.infolist():
            contents = source.read(info)
            if info.filename == 'xl/workbook.xml':
                contents = contents.replace(b'</sheets>', b'<sheet name="Aux" sheetId="2" r:id="rId2"/></sheets>')
            if info.filename == 'xl/_rels/workbook.xml.rels':
                contents = contents.replace(b'</Relationships>', b'<Relationship Id="rId2" Target="worksheets/sheet2.xml" Type="worksheet"/></Relationships>')
            destination.writestr(info.filename, contents)
        auxiliary = '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1"><c r="A1"><f>' + formula + '</f></c></row></sheetData></worksheet>'
        destination.writestr('xl/worksheets/sheet2.xml', auxiliary)
    return output.getvalue()


class WorkbookInspectionTest(unittest.TestCase):
    def test_first_sheet_and_metadata(self):
        result = inspect_workbook(spec(), workbook())
        self.assertEqual((result['sheet'], result['first_row'], result['last_row']), ('Algorithm', 13, 14))

    def test_named_sheet_validation(self):
        with self.assertRaisesRegex(ValueError, '워크시트'):
            inspect_workbook(spec(sheet='Missing'), workbook())

    def test_input_cells_same_row_and_distinct(self):
        with self.assertRaises(ValueError):
            spec(temperature_cell='B14').validate()
        with self.assertRaises(ValueError):
            spec(actual_power_cell='B13').validate()
        with self.assertRaises(ValueError):
            spec(target_cell='B14').validate()

    def test_reject_undeclared_old_layer_values(self):
        with self.assertRaisesRegex(ValueError, '지정하지 않은'):
            inspect_workbook(spec(layer_cell=None), workbook())
        with self.assertRaisesRegex(ValueError, '지정하지 않은'):
            inspect_workbook(spec(), workbook(extra='<c r="E13"><v>44</v></c>'))

    def test_reject_formula_inside_input_history(self):
        with self.assertRaisesRegex(ValueError, '입력 열'):
            inspect_workbook(spec(), workbook(row14='<c r="C14"><f>C13</f></c>'))

    def test_reject_formula_that_uses_a_future_layer(self):
        with self.assertRaisesRegex(ValueError, '미래 층'):
            inspect_workbook(spec(), workbook(recommendation='C13+B14'))

    def test_other_sheet_cannot_reference_target_or_history_ranges(self):
        for formula in ("'Algorithm'!B5", "SUM('Algorithm'!A4:C6)", "'Algorithm'!B13", "SUM('Algorithm'!A2:B20)"):
            with self.assertRaisesRegex(ValueError, '다른 시트'):
                _reject_cross_sheet_history(formula, 'Algorithm', 13, (2, 5))
        _reject_cross_sheet_history("'Algorithm'!B4", 'Algorithm', 13, (2, 5))

    def test_only_dependencies_need_to_be_independent_of_current_history(self):
        unused = add_auxiliary_sheet(workbook(), "'Algorithm'!B5")
        self.assertEqual(inspect_workbook(spec(), unused)['sheet'], 'Algorithm')
        used = add_auxiliary_sheet(workbook(extra='<c r="E13"><f>Aux!A1</f></c>'), "'Algorithm'!B5")
        with self.assertRaisesRegex(ValueError, '다른 시트'):
            inspect_workbook(spec(), used)

    def test_reject_external_link_and_unsafe_formula(self):
        relationship = '<Relationship Id="rId2" Target="https://example.com" TargetMode="External" Type="hyperlink"/>'
        with self.assertRaisesRegex(ValueError, '외부 링크'):
            inspect_workbook(spec(), workbook(relationship=relationship))
        with self.assertRaisesRegex(ValueError, '함수'):
            inspect_workbook(spec(), workbook(recommendation='WEBSERVICE(B13)'))
        with self.assertRaisesRegex(ValueError, '외부 참조'):
            inspect_workbook(spec(), workbook(recommendation='[Other.xlsx]Sheet1!A1'))

    def test_reject_formula_free_recommendation_and_macro(self):
        data = workbook()
        source = zipfile.ZipFile(io.BytesIO(data))
        output = io.BytesIO()
        with source, zipfile.ZipFile(output, 'w') as z:
            for item in source.infolist():
                content = source.read(item)
                if item.filename.endswith('sheet1.xml'):
                    content = content.replace(b'<f>C13+B13</f>', b'')
                z.writestr(item.filename, content)
        with self.assertRaisesRegex(ValueError, '수식'):
            inspect_workbook(spec(), output.getvalue())
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w') as z:
            z.writestr('xl/vbaProject.bin', b'anything')
        with self.assertRaisesRegex(ValueError, '매크로'):
            inspect_workbook(spec(), output.getvalue())


class WorkbookControlTest(unittest.TestCase):
    def controller(self, output):
        c = object.__new__(WorkbookController)
        c.metadata = {'first_row': 13, 'last_row': 14}
        c.target = 65.0
        c._count = 0
        c._last_layer = None
        c.calls = []
        def call(request, timeout=None):
            c.calls.append(request)
            return {'value': output} if request['op'] == 'step' else {'ok': True}
        c._call = call
        return c

    def test_caps_and_target_applies_only_to_new_observation(self):
        c = self.controller(90.0)
        c.set_target_temperature(70)
        self.assertEqual(c.process(101, 65.0, 60.0).power_percent, 65)
        self.assertEqual(c.calls[0]['target'], 70)
        self.assertEqual(c.calls[0]['layer'], 101)
        self.assertEqual(c.calls[0]['index'], 0)

    def test_bad_results_never_advance(self):
        for value in (None, math.inf, math.nan, '20', True, -1, 101):
            c = self.controller(value)
            with self.assertRaises(ValueError):
                c.process(1, 65, 60)
            self.assertEqual(c._count, 0)

    def test_discontinuity_resets_and_layer_count_is_bounded(self):
        c = self.controller(60.0)
        c.process(10, 65, 60)
        c.process(20, 65, 60)
        self.assertEqual([x['op'] for x in c.calls], ['step', 'reset', 'step'])
        c.process(21, 65, 60)
        with self.assertRaisesRegex(ValueError, '초과'):
            c.process(22, 65, 60)

    def test_stop_event_interrupts_response_wait_and_stops_helper(self):
        class FakeProcess:
            def __init__(self):
                self.stdin = io.StringIO()
                self.stopped = False
            def poll(self):
                return 0 if self.stopped else None
            def kill(self):
                self.stopped = True
            def wait(self, timeout=None):
                return 0
        c = object.__new__(WorkbookController)
        process = FakeProcess()
        c._proc, c._queue, c._excel_handle, c._temp = process, queue.Queue(), None, None
        c._lifecycle_lock, c._closed = threading.Lock(), False
        c.timeout = 8
        c.stop_event = threading.Event()
        c.stop_event.set()
        with self.assertRaises(InterruptedError):
            c._call({'op': 'step'})
        self.assertTrue(process.stopped)


if __name__ == '__main__':
    unittest.main()
