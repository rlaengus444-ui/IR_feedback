"""Real-file same-second restart identity and final Python preflight fences."""
from pathlib import Path
import os,tempfile,unittest
from adapter_contracts import MachineWindow,ControlUnavailable
from csv_source import JobLog,JobLogReadUnavailable
import test_windows_ui_adapter as fixture

class JobOccurrenceTests(unittest.TestCase):
    TEXT='2026-11-11오전090000...[BJT - AUTO]: AutoPrint started!\n2026-11-11오전090000...[BJT - AUTO]: Layer: 0 -> Current Z = 1\n'
    def setup_log(self,directory):
        root=Path(directory);(root/'Logs').mkdir();path=root/'Logs/day_M1.log'
        prefix='한글 무관 기록\n'
        path.write_text(prefix+self.TEXT,encoding='utf-8')
        return path,JobLog(MachineWindow('M1',1,2,'fixture',str(root/'Controller.exe'))),len(prefix.encode())
    def test_identical_same_second_start_occurrences_are_distinct(self):
        with tempfile.TemporaryDirectory() as directory:
            path,reader,start=self.setup_log(directory);first=reader.read()
            self.assertEqual(start,first.auto_position)
            with path.open('a',encoding='utf-8') as f:f.write(self.TEXT)
            second=reader.read()
            self.assertNotEqual(first.run_key,second.run_key)
            self.assertEqual(first.auto_started,second.auto_started)
            self.assertEqual(first.log_identity,second.log_identity)
            self.assertGreater(second.auto_position,first.auto_position)
            self.assertEqual(second.run_key,reader.proof(second)['run_key'])
            self.assertEqual(second.run_key,reader.read().run_key)
    def test_split_unicode_start_retains_exact_occurrence_offset(self):
        with tempfile.TemporaryDirectory() as directory:
            path,reader,_=self.setup_log(directory);first=reader.read();position=path.stat().st_size
            raw=self.TEXT.encode();split=raw.index(b'AutoPrint')+5
            with path.open('ab') as f:f.write(raw[:split])
            with self.assertRaises(JobLogReadUnavailable):reader.read()
            with path.open('ab') as f:f.write(raw[split:])
            second=reader.read()
            self.assertNotEqual(first.run_key,second.run_key)
            self.assertEqual(position,second.auto_position)
    def test_replaced_log_incarnation_does_not_inherit_run_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            path,reader,_=self.setup_log(directory);first=reader.read()
            replacement=path.with_suffix('.new');replacement.write_bytes(path.read_bytes());os.replace(replacement,path)
            second=reader.read()
            self.assertNotEqual(first.log_identity,second.log_identity)
            self.assertNotEqual(first.run_key,second.run_key)
    def test_same_second_restart_during_preflight_discards_old_decision(self):
        with tempfile.TemporaryDirectory() as directory:
            raw,state,calls,path,values,append,baseline,source,audit=fixture.AdapterTests().fixture(directory)
            try:
                fresh=append(index=1)
                original_run=raw._run_key
                log=source.parents[2]/'Logs/20261027_M1.log'
                original_start='\n'.join(log.read_text().splitlines()[:2])+'\n'
                with log.open('a') as f:f.write(original_start)
                with self.assertRaises(ControlUnavailable):raw._verify_csv_observation(fresh.observation_key)
                self.assertNotEqual(original_run,raw._csv.log.context.run_key)
                self.assertEqual(0,state['saves'])
                self.assertFalse(any(c['operation']=='apply' for c in calls))
            finally:raw.disconnect()

if __name__=='__main__':unittest.main()
