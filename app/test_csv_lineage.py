"""CSV observation lineage regression tests."""
import csv
from datetime import datetime, timedelta
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from adapter_contracts import MachineWindow
from csv_source import JobContext, MonitoringCsvReader

TARGET = MachineWindow('M1', 11, 12, 'fixture', 'C:/fixture/Operator.exe')


class CsvLineageTests(unittest.TestCase):
    def test_same_semantic_csv_key_cannot_hide_changed_image_or_recoat_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);now=datetime.now().replace(microsecond=0)
            context=JobContext('run',0,now-timedelta(minutes=1),0,'AUTO')
            log=Mock();log.read.return_value=context
            job=root/(context.first_started.strftime('%Y%m%d_%H%M%S')+'_8-layer');job.mkdir()
            path=job/('LayerDatasheet_'+context.first_started.strftime('%Y%m%d_%H%M%S')+'.csv')
            header=list(MonitoringCsvReader.REQUIRED)+['ImgSlicing']
            def encode(row):
                text=io.StringIO(newline='');csv.writer(text).writerows(row);return text.getvalue().encode()
            row=['1',context.first_started.strftime('%m/%d/%Y %H:%M:%S'),
                 (now-timedelta(seconds=20)).strftime('%m/%d/%Y %H:%M:%S'),
                 now.strftime('%m/%d/%Y %H:%M:%S'),'65','C:\\a\\001.png']
            path.write_bytes(encode([header,row]))
            reader=MonitoringCsvReader(TARGET,roots=[root],log=log)
            first=reader.read();self.assertIsNotNone(first.sample)
            changed=list(row);changed[-1]='C:\\b\\001.png'
            with path.open('ab') as f:f.write(encode([changed]))
            second=reader.read()
            self.assertEqual(first.sample.key,second.sample.key)
            self.assertNotEqual(first.sample.slice_lineage,second.sample.slice_lineage)



if __name__=='__main__':unittest.main()
