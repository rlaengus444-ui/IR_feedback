"""Exercise the packaged Windows UI bridge with a Zone.Identifier stream.

Usage: runtime/python.exe release_tools/windows_bridge_probe.py PACKAGE_ROOT OUTPUT_JSON
This probe uses disposable PowerShell 5.1 processes and the package's real adapter.
Run only in a test VM without connected equipment.
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback

PIN = '181d32dd5b13cee276553f6dd2347227fe9b44c288e717d0a399ec57d47b700f'
MAX_BYTES = 1024 * 1024
ZONE = b'[ZoneTransfer]\r\nZoneId=3\r\n'


def digest(data):
    return hashlib.sha256(data).hexdigest()


def powershell(script, dll, helper):
    exe = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    env = os.environ.copy()
    env['BRIDGE_PROBE_DLL'] = str(dll)
    env['BRIDGE_PROBE_HELPER'] = str(helper)
    encoded = base64.b64encode(script.encode('utf-16-le')).decode('ascii')
    result = subprocess.run([str(exe), '-NoLogo', '-NoProfile', '-NonInteractive', '-Mta', '-EncodedCommand', encoded],
                            env=env, capture_output=True, text=True, encoding='utf-8', errors='replace',
                            timeout=30, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        raise RuntimeError(f'PowerShell probe exited {result.returncode}: {result.stderr[-2000:]} {result.stdout[-2000:]}')
    try:
        return json.loads(result.stdout.lstrip('\ufeff').strip())
    except ValueError as exc:
        raise RuntimeError(f'PowerShell probe did not return JSON: {result.stdout[-2000:]} {result.stderr[-2000:]}') from exc


OLD = r'''
$ErrorActionPreference = 'Stop'
$result = @{ succeeded = $false; exception = ''; hresults = @(); reproduced_0x80131515 = $false }
try {
    Add-Type -Path $env:BRIDGE_PROBE_DLL
    $result.succeeded = $true
} catch {
    $result.exception = [string]$_
    $e = $_.Exception
    $codes = @()
    while ($null -ne $e) {
        $hex = '0x{0:X8}' -f ([uint32]([int64]$e.HResult -band 0xFFFFFFFFL))
        $codes += $hex
        $e = $e.InnerException
    }
    $result.hresults = $codes
    $result.reproduced_0x80131515 = ($codes -contains '0x80131515')
}
[Console]::Out.Write(($result | ConvertTo-Json -Depth 5 -Compress))
'''
INITIALIZE = r'''
$ErrorActionPreference = 'Stop'
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($env:BRIDGE_PROBE_HELPER, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw ('Helper parse error: ' + ($errors | Out-String)) }
$nodes = @($ast.FindAll({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -ceq 'ImportBundledBridge' }, $true))
if ($nodes.Count -ne 1) { throw "Expected exactly one ImportBundledBridge function; got $($nodes.Count)" }
. ([scriptblock]::Create($nodes[0].Extent.Text))
ImportBundledBridge $env:BRIDGE_PROBE_DLL
[BinderJetUiAutomationInitialization]::Initialize()
[Console]::Out.Write('{"initialized":true}')
'''


def main(root, output):
    report = {'passed': False, 'checks': {}, 'no_equipment_writes': True}
    dll = root / 'app' / 'UiaBridge.dll'
    helper = root / 'app' / 'native_helper.ps1'
    zone_path = str(dll) + ':Zone.Identifier'
    original = None
    renamed = dll.with_name('UiaBridge.dll.bridge-probe-missing')
    moved = False
    try:
        if os.name != 'nt':
            raise RuntimeError('This probe requires Windows and an ADS-capable filesystem.')
        if Path(sys.executable).resolve() != (root / 'runtime' / 'python.exe').resolve():
            raise RuntimeError('Run with the extracted package runtime/python.exe.')
        if not dll.is_file() or not helper.is_file():
            raise FileNotFoundError('Packaged DLL or native helper missing')
        if renamed.exists():
            raise FileExistsError(f'Refusing to overwrite {renamed}')
        original = dll.read_bytes()
        if len(original) != 21504 or digest(original) != PIN:
            raise ValueError('Packaged DLL original bytes or pinned hash do not match')
        report['source_dll_sha256_before'] = digest(original)
        report['checks']['original_hash_and_size'] = True
        # Write and read back the real NTFS stream; missing ADS support is a failure.
        with open(zone_path, 'wb') as stream:
            stream.write(ZONE)
        def check_zone():
            with open(zone_path, 'rb') as stream:
                value = stream.read()
            if value != ZONE:
                raise AssertionError(f'Zone.Identifier changed: {value!r}')
            return value.decode('ascii')
        report['ads_before'] = check_zone()
        report['checks']['ads_supported_and_zone_3'] = True
        report['old_loader'] = powershell(OLD, dll, helper)
        check_zone()

        # Import the extracted package's actual module, not any repository copy.
        app = (root / 'app').resolve()
        sys.path.insert(0, str(app))
        import windows_ui_adapter as adapter
        if Path(adapter.__file__).resolve() != app / 'windows_ui_adapter.py':
            raise RuntimeError('Imported adapter outside package')
        if adapter._HELPER:
            raise RuntimeError('Packaged adapter does not use its adjacent production helper')
        started = time.monotonic()
        result = adapter.WindowsUiAdapter().discover()
        report['new_discover_seconds'] = time.monotonic() - started
        if not isinstance(result, list):
            raise AssertionError('discover did not return a list')
        report['new_discover_count'] = len(result)
        report['checks']['actual_adapter_discover'] = True
        check_zone()
        report.update(powershell(INITIALIZE, dll, helper))
        if report.get('initialized') is not True:
            raise AssertionError('Actual bridge initialization did not succeed')
        report['checks']['actual_bridge_initialize'] = True
        check_zone()

        def expect_failure(label, required):
            try:
                adapter.WindowsUiAdapter(helper_timeout=10).discover()
            except adapter.AdapterError as exc:
                message = str(exc)
                if not any(token in message.casefold() for token in required):
                    raise AssertionError(f'{label} failed for an unexpected reason: {message}') from exc
                report[label] = message
                report['checks'][label] = True
            else:
                raise AssertionError(f'{label} unexpectedly succeeded')

        modified = bytearray(original)
        modified[-1] ^= 1
        with dll.open('r+b') as stream:
            stream.write(modified)
            stream.truncate()
        check_zone()
        expect_failure('tampered_rejected', ('sha-256 verification failed',))
        check_zone()
        with dll.open('r+b') as stream:
            stream.write(b'X' * (MAX_BYTES + 1))
            stream.truncate()
        check_zone()
        expect_failure('oversize_rejected', ('exceeds its verified size bound',))
        check_zone()
        # Rename preserves the marked file and its stream while making the path absent.
        dll.rename(renamed)
        moved = True
        expect_failure('missing_rejected', ('bundled ui bridge file is missing',))
        renamed.rename(dll)
        moved = False
        check_zone()
    except Exception as exc:
        report['error'] = str(exc)
        report['traceback'] = traceback.format_exc()
    finally:
        try:
            if moved:
                renamed.rename(dll)
            if original is not None:
                with dll.open('r+b') as stream:
                    stream.write(original)
                    stream.truncate()
                report['source_dll_sha256_after'] = digest(dll.read_bytes())
                if report['source_dll_sha256_after'] != PIN:
                    raise AssertionError('Failed to restore exact DLL bytes')
                report['checks']['dll_bytes_restored'] = True
                with open(zone_path, 'rb') as stream:
                    zone_after = stream.read()
                report['ads_after'] = zone_after.decode('ascii')
                if zone_after != ZONE:
                    raise AssertionError('Zone.Identifier was not preserved')
                report['checks']['ads_preserved'] = True
        except Exception as exc:
            report['cleanup_error'] = str(exc)
            report['cleanup_traceback'] = traceback.format_exc()
        required = {'original_hash_and_size', 'ads_supported_and_zone_3', 'actual_adapter_discover',
                    'actual_bridge_initialize', 'tampered_rejected', 'oversize_rejected', 'missing_rejected',
                    'dll_bytes_restored', 'ads_preserved'}
        report['passed'] = not report.get('error') and not report.get('cleanup_error') and all(report['checks'].get(k) for k in required)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    raise SystemExit(main(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()))
