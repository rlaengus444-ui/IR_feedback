"""Copy the bundled editable workbook and optionally fill advisory image inputs."""
from pathlib import Path
import os
import tempfile
import zipfile
from xml.etree import ElementTree as ET
from slice_data import FEATURE_NAMES

TEMPLATE = Path(__file__).with_name('templates') / 'IR_슬라이싱_수동계산.xlsx'
NS = {'m': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
ET.register_namespace('', NS['m'])
ET.register_namespace('r', 'http://schemas.openxmlformats.org/officeDocument/2006/relationships')


def save_workbook(path, dataset=None):
    destination = Path(path)
    if destination.resolve() == TEMPLATE.resolve():
        raise ValueError('프로그램의 원본 템플릿을 덮어쓸 수 없습니다.')
    temporary = None
    try:
        with zipfile.ZipFile(TEMPLATE) as source:
            changes = {}
            if dataset is not None:
                root = ET.fromstring(source.read('xl/worksheets/sheet1.xml'))
                rows = {int(r.get('r')): r for r in root.findall('m:sheetData/m:row', NS)}
                first, last = dataset.records[0].layer, dataset.records[-1].layer
                if last - first + 1 > 3000:
                    raise ValueError('분석값 포함 수동시트는 최대 3000개 층 범위입니다. CSV로 전체를 내보낼 수 있습니다.')
                for number in range(13, 3013):
                    row = rows[number]
                    layer = first + number - 13
                    values = dataset.features(layer)
                    cells = {c.get('r'): c for c in row.findall('m:c', NS)}
                    # A = actual layer, BL:BW = immutable advisory inputs.
                    pairs = [('A', layer)] + [(f'B{chr(ord("L") + i)}', values[name]) for i, name in enumerate(FEATURE_NAMES)]
                    for column, value in pairs:
                        address = column + str(number)
                        cell = cells.get(address)
                        if cell is None:
                            cell = ET.SubElement(row, '{'+NS['m']+'}c', {'r': address})
                        cell.attrib.pop('t', None)
                        for child in list(cell):
                            cell.remove(child)
                        if value is not None:
                            ET.SubElement(cell, '{'+NS['m']+'}v').text = str(value)
                changes['xl/worksheets/sheet1.xml'] = ET.tostring(root, encoding='utf-8', xml_declaration=True)
            with tempfile.NamedTemporaryFile(dir=destination.parent, suffix='.xlsx', delete=False) as handle:
                temporary = handle.name
            with zipfile.ZipFile(temporary, 'w') as output:
                for info in source.infolist():
                    output.writestr(info, changes.get(info.filename, source.read(info)))
        os.replace(temporary, destination)
        temporary = None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
