"""Windows-only synthetic discovery regression probe.

Usage: python release_tools/windows_discovery_probe.py PACKAGE_ROOT OUTPUT_JSON [OLD_DLL]
Builds a disposable fixture with the Windows framework compiler in a temporary directory.
No existing application windows are enumerated; no heater edit or Save is performed.
"""
import base64
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import traceback

PS = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
SOURCE = Path(__file__).resolve().parents[1] / 'validation' / 'NativeDiscoveryFixture.cs'
FLAGS = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


def run_ps(script, env, timeout=45):
    encoded = base64.b64encode(script.encode('utf-16-le')).decode('ascii')
    result = subprocess.run([str(PS), '-NoLogo', '-NoProfile', '-NonInteractive', '-Mta', '-EncodedCommand', encoded],
                            env=env, capture_output=True, text=True, encoding='utf-8', errors='replace',
                            timeout=timeout, creationflags=FLAGS)
    if result.returncode:
        raise RuntimeError('PowerShell failed: ' + result.stderr[-2000:] + result.stdout[-2000:])
    return json.loads(result.stdout.lstrip('\ufeff').strip())


DIRECT = r'''
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes, Accessibility
[void][Reflection.Assembly]::Load([IO.File]::ReadAllBytes($env:PROBE_DLL))
$root = [IntPtr][long]$env:PROBE_HWND; $pidTarget = [int]$env:PROBE_PID
$r = @{}
foreach ($name in @('machine','navigation','metadata','wrong_pid')) {
  try {
    $v = switch ($name) {
      'machine' { [BinderJetNativeProcess]::IdentifyMachine($root,$pidTarget) }
      'navigation' { [BinderJetNativeProcess]::FindNavigation($root,$pidTarget,'btnSequences').ToInt64() }
      'metadata' {
        if ($env:PROBE_MODE -like 'limit*') {
          $method = [BinderJetNativeProcess].GetMethod('VisibleLeafHandles', [Reflection.BindingFlags]'Static,NonPublic')
          if ($null -eq $method) { throw 'Shared native leaf policy not found' }
          @($method.Invoke($null, @($root, $pidTarget, $true))).Count
        } else { @([BinderJetLeafMetadata]::Collect($root,$pidTarget)).Count }
      }
      'wrong_pid' { [BinderJetNativeProcess]::IdentifyMachine($root,($pidTarget+1)) }
    }
    $r[$name] = @{success=$true;value=$v}
  } catch { $r[$name] = @{success=$false;error=[string]$_.Exception} }
}
[Console]::Out.Write(($r | ConvertTo-Json -Depth 5 -Compress))
'''


STALE = r'''
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes, Accessibility
[void][Reflection.Assembly]::Load([IO.File]::ReadAllBytes($env:PROBE_DLL))
$method = [BinderJetNativeProcess].GetMethod('ValidateVisibleLeaf', [Reflection.BindingFlags]'Static,NonPublic')
if ($null -eq $method) { throw 'Visible-leaf validation not found' }
try {
    $v = $method.Invoke($null, @([IntPtr][long]$env:PROBE_HWND, [IntPtr][long]$env:PROBE_STALE, [int]$env:PROBE_PID, $true))
    $r = @{success=$true;value=[string]$v}
} catch { $r = @{success=$false;error=[string]$_.Exception} }
[Console]::Out.Write(($r | ConvertTo-Json -Compress))
'''


def helper(helper_path, dll, hwnd, pid, operation, metadata=None, timeout=45):
    req = {'operation': operation, 'bridge_path': str(dll), 'hwnd': hwnd, 'pid': pid, 'machine': 'S3', 'handles': {}}
    if metadata is not None:
        req['metadata'] = metadata
    p = subprocess.run([str(PS), '-NoLogo', '-NoProfile', '-NonInteractive', '-Mta', '-File', str(helper_path)],
                       input=json.dumps(req), capture_output=True, text=True, encoding='utf-8', errors='replace',
                       timeout=timeout, creationflags=FLAGS)
    try:
        return json.loads(p.stdout.lstrip('\ufeff').strip())
    except ValueError as e:
        raise RuntimeError('Helper response invalid: ' + p.stdout[-2000:] + p.stderr[-2000:]) from e


def main(root, output, old):
    report = {'passed': False, 'checks': {}, 'cases': {}, 'no_equipment_writes': True,
              'boundary_check': 'Shared VisibleLeafHandles(includeEdits=True), not 900 UIA Collect calls',
              'untested': ['Raw 16384 HWND overflow: normal per-process Windows USER handle quota prevents creating enough windows; no synthetic substitute claimed.']}
    try:
        if os.name != 'nt':
            raise RuntimeError('Windows is required')
        dll, helper_path = root / 'app/UiaBridge.dll', root / 'app/native_helper.ps1'
        report['bridge_sha256'] = hashlib.sha256(dll.read_bytes()).hexdigest()
        compiler = Path(os.environ['WINDIR']) / 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
        with tempfile.TemporaryDirectory(prefix='native-discovery-') as tmp:
            fixture = Path(tmp) / 'NativeDiscoveryFixture.exe'
            subprocess.run([str(compiler), '/nologo', '/target:winexe', '/platform:x64', '/out:' + str(fixture),
                            '/reference:System.Windows.Forms.dll', '/reference:System.Drawing.dll', str(SOURCE)],
                           check=True, capture_output=True, timeout=30)
            def case(mode):
                marker = Path(tmp) / mode
                proc = subprocess.Popen([str(fixture), str(marker), mode], creationflags=FLAGS)
                try:
                    ready = Path(str(marker) + '.ready')
                    end = time.monotonic() + 45
                    while not ready.exists() and time.monotonic() < end:
                        if proc.poll() is not None:
                            raise RuntimeError('Fixture exited before ready: ' + mode)
                        time.sleep(.1)
                    if not ready.exists():
                        raise TimeoutError('Fixture did not become ready: ' + mode)
                    pid, hwnd = map(int, ready.read_text().split(','))
                    if pid != proc.pid:
                        raise AssertionError('Fixture PID mismatch')
                    env = os.environ.copy()
                    env.update(PROBE_DLL=str(dll), PROBE_PID=str(pid), PROBE_HWND=str(hwnd), PROBE_MODE=mode)
                    result = {'durations_seconds': {}}
                    def timed(name, fn):
                        start = time.monotonic()
                        try: return fn()
                        finally: result['durations_seconds'][name] = time.monotonic() - start
                    result['direct'] = timed('direct', lambda: run_ps(DIRECT, env, 120))
                    if mode == 'bulk':
                        result['enter_sequence'] = timed('enter_sequence', lambda: helper(helper_path, dll, hwnd, pid, 'enter_sequence', timeout=3))
                    if mode in ('bulk', 'duplicate_id'):
                        result['metadata'] = timed('metadata', lambda: helper(helper_path, dll, hwnd, pid, 'metadata', timeout=3))
                        if result['metadata'].get('success'):
                            result['prepare'] = timed('prepare', lambda: helper(helper_path, dll, hwnd, pid, 'prepare_control', result['metadata']['records'], timeout=5))
                    if mode == 'bulk' and old:
                        env['PROBE_DLL'] = str(old)
                        result['old_direct'] = timed('old_direct', lambda: run_ps(DIRECT, env, 120))
                    if mode.startswith('stale_'):
                        env['PROBE_STALE'] = Path(str(marker)+'.stale').read_text()
                        result['stale_before'] = timed('stale_before', lambda: run_ps(STALE, env))
                        Path(str(marker)+'.command').write_text(mode[6:])
                        deadline = time.monotonic() + 5
                        done = Path(str(marker)+'.done')
                        while not done.exists() and time.monotonic() < deadline: time.sleep(.05)
                        if not done.exists(): raise TimeoutError('Fixture mutation timed out: ' + mode)
                        result['stale_after'] = timed('stale_after', lambda: run_ps(STALE, env))
                    heartbeat = Path(str(marker) + '.heartbeat')
                    deadline = time.monotonic() + 3
                    while not heartbeat.exists() and time.monotonic() < deadline:
                        time.sleep(.1)
                    first = heartbeat.read_text() if heartbeat.exists() else ''
                    time.sleep(.6)
                    second = heartbeat.read_text() if heartbeat.exists() else ''
                    result['heartbeat_progress'] = bool(first and second and first != second)
                    result['ir_and_saves'] = second.split(',')[1:] if second else []
                    result['unsupported_messages'] = Path(str(marker)+'.poison').read_text() if Path(str(marker)+'.poison').exists() else ''
                    startup = Path(str(marker)+'.startup-poison')
                    result['startup_message_counts'] = dict(Counter(startup.read_text().splitlines())) if startup.exists() else {}
                    result['navigation_clicks'] = Path(str(marker)+'.nav-clicks').read_text() if Path(str(marker)+'.nav-clicks').exists() else '0'
                    return result
                finally:
                    proc.terminate()
                    try: proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        proc.kill(); proc.wait(timeout=10)
            for mode in ('bulk', 'conflict', 'duplicate_nav', 'duplicate_id', 'limit900', 'limit901', 'stale_hide', 'stale_reparent', 'stale_destroy'):
                report['cases'][mode] = case(mode)
            cases, checks = report['cases'], report['checks']
            bulk = cases['bulk']; d = bulk['direct']
            checks['bulk_machine_and_navigation'] = d['machine'].get('value') == 'S3' and d['navigation'].get('success')
            checks['actual_enter_sequence_succeeds'] = bulk['enter_sequence'].get('success') and bulk['enter_sequence'].get('entered') and bulk['navigation_clicks'] == '1'
            checks['wrong_pid_rejected'] = not d['wrong_pid']['success']
            checks['bulk_metadata_and_prepare'] = bulk['metadata'].get('success') and bulk.get('prepare', {}).get('success') and str(bulk['prepare'].get('setting')) == '62'
            checks['disabled_field_included'] = (str(bulk.get('prepare', {}).get('fields', {}).get('textBoxUnrelated')) == '5' and
                any(r.get('id') == 'textBoxUnrelated' and r.get('visible') and not r.get('enabled', True)
                    for r in bulk.get('metadata', {}).get('records', [])))
            checks['stale_visible_leaf_rejected'] = all(cases['stale_'+name]['stale_before']['success'] and not cases['stale_'+name]['stale_after']['success'] for name in ('hide', 'reparent', 'destroy'))
            checks['late_conflicting_machine_rejected'] = not cases['conflict']['direct']['machine']['success']
            checks['duplicate_navigation_rejected'] = not cases['duplicate_nav']['direct']['navigation']['success']
            checks['duplicate_id_rejected'] = cases['duplicate_id']['metadata'].get('success') and not cases['duplicate_id'].get('prepare', {}).get('success', True)
            checks['shared_leaf_selector_900_accepted'] = cases['limit900']['direct']['metadata'].get('value') == 900
            checks['shared_leaf_selector_901_rejected'] = (not cases['limit901']['direct']['metadata']['success'] and
                                                '900' in cases['limit901']['direct']['metadata'].get('error', ''))
            checks['fixtures_responsive_and_unchanged'] = all(c['heartbeat_progress'] and c['ir_and_saves'] == ['62', '0'] for c in cases.values())
            checks['hidden_and_unsupported_controls_not_probed'] = all(not c['unsupported_messages'] for c in cases.values())
            if old:
                checks['old_900_child_failure_reproduced'] = not bulk['old_direct']['machine']['success'] and '900' in bulk['old_direct']['machine'].get('error', '')
            else:
                report['untested'].append('Old DLL reproduction omitted: no OLD_DLL supplied.')
            report['passed'] = all(checks.values())
    except Exception as exc:
        report['error'] = str(exc); report['traceback'] = traceback.format_exc()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, indent=2))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    if len(sys.argv) not in (3, 4):
        raise SystemExit(__doc__)
    raise SystemExit(main(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve(), Path(sys.argv[3]).resolve() if len(sys.argv) == 4 else None))
