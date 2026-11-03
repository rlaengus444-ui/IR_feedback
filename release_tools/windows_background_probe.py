"""Windows-only synthetic end-to-end background Save probe.

Usage: python windows_background_probe.py PACKAGE_ROOT OUTPUT_JSON
Uses only self-created fixture processes and temporary files; never equipment.
"""
import ctypes
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import traceback

SOURCE = Path(__file__).resolve().parents[1] / 'validation' / 'CsvRouteFixture.cs'
HARNESS = Path(__file__).resolve().parents[1] / 'validation' / 'csv_route_probe.py'
COVER = r'''
using System;
using System.IO;
using System.Windows.Forms;
class Cover {
 [STAThread] static void Main(string[] args) {
  var f = new Form { Text="Synthetic foreground browser", Width=1000, Height=700,
                     StartPosition=FormStartPosition.CenterScreen, TopMost=true };
  f.Controls.Add(new Label { Text="Synthetic unrelated application", Dock=DockStyle.Fill });
  f.Shown += delegate { f.Activate(); File.WriteAllText(args[0],
      System.Diagnostics.Process.GetCurrentProcess().Id + "," + f.Handle.ToInt64()); };
  Application.Run(f);
 }
}
'''


def main(root, output):
    report = {'passed': False, 'checks': {}, 'cases': [],
              'scope': 'Synthetic WinForms target, CSV and temporary owner files only; production session, adapter, bridge and native Save transport.',
              'untested': ['Real BJT, hardware, thermal response, locked desktop and hidden windows.']}
    cover = None
    sampler_stop = threading.Event()
    samples = []
    try:
        if os.name != 'nt':
            raise RuntimeError('Windows is required')
        sys.path.insert(0, str(root / 'app'))
        compiler = Path(os.environ['WINDIR']) / 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
        with tempfile.TemporaryDirectory(prefix='background-proof-') as directory:
            folder = Path(directory)
            (folder / 'fullroutebin').mkdir()
            exe = folder / 'fullroutebin/CsvRouteFixture.exe'
            cover_exe = folder / 'cover.exe'
            cover_source = folder / 'cover.cs'
            cover_source.write_text(COVER, encoding='utf-8')
            for target, source, refs in ((exe, SOURCE, ('System.Windows.Forms.dll', 'System.Drawing.dll', 'System.Web.Extensions.dll')),
                                         (cover_exe, cover_source, ('System.Windows.Forms.dll', 'System.Drawing.dll'))):
                subprocess.run([str(compiler), '/nologo', '/target:winexe', '/platform:x64', '/out:' + str(target)]
                    + ['/reference:' + ref for ref in refs] + [str(source)], check=True, capture_output=True, timeout=30)
            user32 = ctypes.windll.user32
            user32.GetForegroundWindow.restype = ctypes.c_void_p
            def start_cover(target_pid):
                nonlocal cover, samples
                ready = folder / 'cover.ready'
                ready.unlink(missing_ok=True)
                cover = subprocess.Popen([str(cover_exe), str(ready)])
                deadline = time.monotonic() + 10
                while not ready.exists() and time.monotonic() < deadline:
                    if cover.poll() is not None: raise RuntimeError('Cover process exited')
                    time.sleep(.05)
                if not ready.exists(): raise TimeoutError('Cover did not start')
                pid, hwnd = map(int, ready.read_text().split(','))
                if pid != cover.pid or pid == target_pid: raise AssertionError('Cover PID identity mismatch')
                deadline = time.monotonic() + 5
                while user32.GetForegroundWindow() != hwnd and time.monotonic() < deadline: time.sleep(.05)
                if user32.GetForegroundWindow() != hwnd: raise AssertionError('Cover did not obtain foreground')
                samples = []
                sampler_stop.clear()
                def sample():
                    while not sampler_stop.wait(.02): samples.append(user32.GetForegroundWindow() == hwnd)
                thread = threading.Thread(target=sample, daemon=True)
                thread.start()
                return thread, hwnd
            def stop_cover(thread, hwnd):
                nonlocal cover
                sampler_stop.set(); thread.join(2)
                result = {'different_process_foreground': bool(samples) and all(samples),
                          'foreground_samples': len(samples),
                          'foreground_at_end': user32.GetForegroundWindow() == hwnd}
                cover.terminate(); cover.wait(timeout=5); cover = None
                return result
            # Reuse the complete synthetic CSV/session/adapter test while placing all
            # fixtures and temporary settings under our private directory.
            source = HARNESS.read_text(encoding='utf-8')
            source = source.replace('base=Path(__file__).resolve().parent', 'base=Path(' + repr(str(folder)) + ')')
            source = source.replace('sys.path.insert(0,str(next(path for path in app_candidates if (path/"windows_ui_adapter.py").is_file())))',
                                    'sys.path.insert(0, ' + repr(str(root / 'app')) + ')')
            source = source.replace('target=MachineWindow(machine,pid,hwnd,"Own CSV fixture",str(exe.resolve()))',
                'assert pid == fixture.pid, "Fixture PID mismatch"\n        target=MachineWindow(machine,pid,hwnd,"Own CSV fixture",str(exe.resolve()))')
            source = source.replace('options=SessionOptions(target,mode="control")',
                'cover_thread, cover_hwnd = start_cover(pid)\n        options=SessionOptions(target,mode="control")')
            source = source.replace('command(page=page,minimized=minimized)\n',
                'command(page=page,minimized=minimized)\n            if case=="paused":command(state="IDLE")\n')
            source = source.replace('expected_saves=0 if case in {"edithang","statechange","statushang"} else 1',
                'expected_saves=0 if case in {"edithang","statechange","statushang","paused"} else 1')
            source = source.replace('if case not in {"savehang","edithang","statechange","statushang"}:assert current.power==67,current',
                'if case not in {"savehang","edithang","statechange","statushang","paused"}:assert current.power==67,current\n            if case=="paused":assert current.power==62,current')
            source = source.replace('print(json.dumps(result),flush=True);return result',
                'result.update(stop_cover(cover_thread, cover_hwnd)); return result')
            namespace = {'__file__': str(folder / 'harness.py'), '__name__': 'background_fixture_harness',
                         'start_cover': start_cover, 'stop_cover': stop_cover}
            exec(compile(source, str(HARNESS), 'exec'), namespace)
            for machine in ('M1', 'S3'):
                for minimized, case in ((False, 'normal'), (True, 'normal'), (False, 'dirty'), (False, 'paused')):
                    try:
                        result = namespace['exercise'](machine, 'webcam', minimized, case)
                        result['passed'] = bool(result['passed'] and result['different_process_foreground'] and result['foreground_at_end'])
                    except Exception as error:
                        result = {'machine': machine, 'minimized': minimized, 'case': case,
                                  'passed': False, 'error': str(error), 'traceback': traceback.format_exc()}
                        sampler_stop.set()
                        if cover is not None:
                            cover.terminate(); cover.wait(timeout=5); cover = None
                    report['cases'].append(result)
            report['checks']['eight_fixture_cases_passed'] = len(report['cases']) == 8 and all(c['passed'] for c in report['cases'])
            report['checks']['four_background_native_saves'] = sum(c.get('saveCount') == 1 and c['passed'] for c in report['cases']) == 4
            report['checks']['four_dirty_or_paused_hold_without_save'] = sum(c.get('saveCount') == 0 and c['passed'] for c in report['cases']) == 4
            report['bridge_sha256'] = hashlib.sha256((root / 'app/UiaBridge.dll').read_bytes()).hexdigest()
            report['helper_sha256'] = hashlib.sha256((root / 'app/native_helper.ps1').read_bytes()).hexdigest()
            report['passed'] = all(report['checks'].values())
    except Exception as error:
        report['error'] = str(error); report['traceback'] = traceback.format_exc()
    finally:
        sampler_stop.set()
        if cover is not None:
            cover.terminate(); cover.wait(timeout=5)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    if len(sys.argv) != 3: raise SystemExit(__doc__)
    raise SystemExit(main(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()))
