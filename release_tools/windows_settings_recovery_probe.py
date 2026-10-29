"""Read-only native recovery probe against an isolated synthetic WinForms process.

Usage: python release_tools/windows_settings_recovery_probe.py PACKAGE_ROOT OUTPUT_JSON
The fixture alone changes its own test fields; no equipment, external settings,
Sequence navigation, automatic IR edit, or Save is requested by this probe.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import traceback
import threading

_ADAPTER = None

PS = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
SOURCE = Path(__file__).resolve().parents[1] / 'validation' / 'SettingsRecoveryFixture.cs'
FLAGS = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


def helper(helper_path, dll, hwnd, pid, operation, handles=None, field_map=None, metadata=None):
    request = {'operation': operation, 'bridge_path': str(dll), 'hwnd': hwnd, 'pid': pid,
               'machine': 'M1', 'handles': handles or {}, 'field_map': field_map or {}}
    if metadata is not None:
        request['metadata'] = metadata
    # Exercise the shipped adapter transport/deadline, not an easier direct -File call.
    from adapter_contracts import AdapterError, ControlSuspended, TargetIdentityError
    started = time.monotonic()
    try:
        result = _ADAPTER._request(request, threading.Event())
    except AdapterError as error:
        result = {'success': False, 'error': str(error), 'error_kind':
                  'control_wait' if isinstance(error, ControlSuspended) else
                  'identity' if isinstance(error, TargetIdentityError) else 'operation'}
    result['transport_seconds'] = time.monotonic() - started
    result['production_adapter_transport'] = True
    return result


def full_profile(executable, folder, machine):
    """Real adapter transport, full schema and temporary owner file; never Save."""
    from adapter_contracts import MachineWindow
    from windows_ui_adapter import WindowsUiAdapter
    from sequence_field_audit import field_bindings, assert_fields_match_persisted
    import sequence_persistence
    bindings = field_bindings(machine)
    spec = folder / (machine + '.fields')
    spec.write_text('\n'.join(name + '\t' + kind for name, (_, _, kind) in bindings.items()))
    owner = folder / 'Machines' / machine / 'Configs' / 'BJT.parameters.json'
    owner.parent.mkdir(parents=True)
    entries = [{'Scope': scope, 'Name': name,
                'Type': 'BoolParameter' if kind == 'toggle' else 'DoubleParameter',
                'Value': '62' if field == 'textBoxHeatingOutput' else 'False' if kind == 'toggle' else '0'}
               for field, (scope, name, kind) in bindings.items()]
    owner.write_text(json.dumps(entries), encoding='utf-8')
    original_sha = hashlib.sha256(owner.read_bytes()).hexdigest()
    marker = folder / ('full-' + machine)
    process = subprocess.Popen([str(executable), str(marker), str(spec), machine], creationflags=FLAGS)
    adapter = WindowsUiAdapter(source_factory=lambda _target: None)
    result = {'machine': machine, 'bound_fields': len(bindings), 'passed': False,
              'scope': 'Full synthetic UI and temporary settings file; no physical equipment or Excel.'}
    try:
        ready = Path(str(marker) + '.ready')
        deadline = time.monotonic() + 20
        while not ready.exists() and time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError('Full-profile fixture exited')
            time.sleep(.05)
        if not ready.exists():
            raise TimeoutError('Full-profile fixture did not become ready')
        pid, hwnd = map(int, ready.read_text().split(','))
        if pid != process.pid:
            raise RuntimeError('Full-profile PID mismatch')
        target = MachineWindow(machine, pid, hwnd, 'Own full-profile fixture', str(executable))
        stop = threading.Event()
        adapter.connect(target, stop)  # Read-only validation, no prepare_mode/navigation.
        adapter._mode = 'control'
        adapter.suspend_control('Synthetic settings recovery')
        durations, epochs = [], []
        for _ in range(2):
            before = time.monotonic()
            adapter._recover_control(target, adapter._generation, stop)
            durations.append(time.monotonic() - before)
            epochs.append(adapter._control_epoch)
            time.sleep(1.05)  # Two coherent confirmations, not a shortened test interval.
        result.update(recovery_seconds=durations, epochs=epochs,
                      hold_reason=adapter._control_hold_reason)
        assert_fields_match_persisted(adapter._control_baseline, sequence_persistence.capture(target), machine)
        heartbeat = Path(str(marker) + '.heartbeat').read_text().split(',')
        poison = Path(str(marker) + '.poison')
        result.update(recovery_seconds=durations, epochs=epochs,
            inventoried_fields=len(adapter._native_fields), hold_reason=adapter._control_hold_reason,
            settings_unchanged=hashlib.sha256(owner.read_bytes()).hexdigest() == original_sha,
            zero_save_and_navigation=heartbeat[-2:] == ['0', '0'],
            process_panel_not_probed=not poison.exists() or not poison.read_text())
        result['passed'] = (epochs == [0, 1] and adapter._control_hold_reason is None
            and len(adapter._native_fields) == len(bindings) and result['settings_unchanged']
            and result['zero_save_and_navigation'] and result['process_panel_not_probed'])
    except Exception as error:
        result['error'] = str(error)
    finally:
        adapter.disconnect()
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill(); process.wait(timeout=5)
    return result



def main(root, output):
    report = {'passed': False, 'checks': {}, 'responses': {}, 'no_equipment_writes': True,
              'scope': 'Synthetic fixture only; fixture changes its own test fields. No navigation or Save requested.',
              'untested': ['Live BJT, hardware, real recipe persistence, and equipment operation.']}
    proc = None
    tmp = None
    try:
        if os.name != 'nt':
            raise RuntimeError('Windows is required')
        global _ADAPTER
        sys.path.insert(0, str(root / 'app'))
        from windows_ui_adapter import WindowsUiAdapter
        _ADAPTER = WindowsUiAdapter()  # Default production deadline; no connect/navigation.
        report['adapter_default_helper_timeout_seconds'] = _ADAPTER.helper_timeout
        dll, helper_path = root / 'app/UiaBridge.dll', root / 'app/native_helper.ps1'
        report['bridge_sha256'] = hashlib.sha256(dll.read_bytes()).hexdigest()
        report['helper_sha256'] = hashlib.sha256(helper_path.read_bytes()).hexdigest()
        compiler = Path(os.environ['WINDIR']) / 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
        tmp = tempfile.TemporaryDirectory(prefix='native-recovery-')
        folder = Path(tmp.name)
        executable = folder / 'SettingsRecoveryFixture.exe'
        subprocess.run([str(compiler), '/nologo', '/target:winexe', '/platform:x64', '/out:' + str(executable),
            '/reference:System.Windows.Forms.dll', '/reference:System.Drawing.dll', str(SOURCE)],
            check=True, capture_output=True, timeout=30)
        marker = folder / 'fixture'
        proc = subprocess.Popen([str(executable), str(marker)], creationflags=FLAGS)
        ready = Path(str(marker) + '.ready')
        deadline = time.monotonic() + 20
        while not ready.exists() and time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError('Synthetic fixture exited before readiness')
            time.sleep(.05)
        if not ready.exists():
            raise TimeoutError('Synthetic fixture did not become ready')
        pid, hwnd = map(int, ready.read_text().split(','))
        if pid != proc.pid:
            raise RuntimeError('Synthetic fixture PID mismatch')
        def command(action):
            done = Path(str(marker) + '.done')
            done.unlink(missing_ok=True)
            path = Path(str(marker) + '.command')
            temporary = Path(str(marker) + '.command.tmp')
            temporary.write_text(action)
            os.replace(temporary, path)
            end = time.monotonic() + 5
            while not done.exists() and time.monotonic() < end:
                time.sleep(.05)
            if not done.exists() or done.read_text() != action:
                raise TimeoutError('Fixture command failed: ' + action)
        def call(operation, handles=None, mapping=None, target_pid=None, metadata=None):
            return helper(helper_path, dll, hwnd, pid if target_pid is None else target_pid,
                          operation, handles, mapping, metadata)
        responses, checks = report['responses'], report['checks']
        responses['metadata'] = call('metadata')
        metadata = responses['metadata'].get('records', [])
        responses['prepare'] = call('prepare_control', metadata=metadata)
        prepared = responses['prepare']
        if not prepared.get('success'):
            raise RuntimeError('Synthetic control metadata did not prepare: ' + str(prepared))
        handles, mapping = prepared['handles'], prepared['field_map']
        checks['initial_fixture_prepared'] = str(prepared.get('setting')) == '62' and bool(mapping)
        responses['cached'] = call('recover_control', handles, mapping)
        checks['cached_read_only_recovery'] = responses['cached'].get('success') and str(responses['cached'].get('state')).upper() == 'AUTO' and str(responses['cached'].get('setting')) == '62'
        command('set')
        responses['manual_values'] = call('recover_control', handles, mapping)
        checks['manual_fixture_changes_visible'] = responses['manual_values'].get('success') and str(responses['manual_values'].get('setting')) == '67' and str(responses['manual_values'].get('fields', {}).get('textBoxSpeed')) == '9'
        command('duplicate_in_place')
        responses['duplicate_in_place'] = call('recover_control', handles, mapping)
        checks['in_place_duplicate_holds'] = not responses['duplicate_in_place'].get('success') and responses['duplicate_in_place'].get('error_kind') == 'control_wait'
        command('remove_extra')
        command('extra_in_place')
        responses['extra_in_place'] = call('recover_control', handles, mapping)
        checks['in_place_new_input_inventoried'] = (responses['extra_in_place'].get('success')
            and 'textBoxUnexpected' in responses['extra_in_place'].get('field_map', {})
            and 'textBoxUnexpected' in responses['extra_in_place'].get('fields', {}))
        command('remove_extra')
        command('pause')
        responses['paused'] = call('recover_control', handles, mapping)
        checks['paused_holds'] = not responses['paused'].get('success') and responses['paused'].get('error_kind') == 'control_wait'
        command('auto')
        command('focus')
        responses['editing'] = call('recover_control', handles, mapping)
        checks['active_edit_holds'] = not responses['editing'].get('success') and responses['editing'].get('error_kind') == 'control_wait'
        command('clear')
        command('missing')
        responses['missing'] = call('recover_control', handles, mapping)
        checks['missing_sequence_holds'] = not responses['missing'].get('success') and responses['missing'].get('error_kind') == 'control_wait'
        command('recreate')
        responses['rebound'] = call('recover_control', handles, mapping)
        rebound = responses['rebound']
        checks['readonly_rebind'] = rebound.get('success') and rebound.get('handles', {}).get('scope') != handles.get('scope') and bool(rebound.get('field_map')) and str(rebound.get('setting')) == '62'
        new_handles, new_map = rebound.get('handles', {}), rebound.get('field_map', {})
        command('duplicate')
        responses['ambiguous'] = call('recover_control', new_handles, new_map)
        checks['ambiguous_ir_holds'] = not responses['ambiguous'].get('success') and responses['ambiguous'].get('error_kind') == 'control_wait'
        responses['wrong_pid'] = call('recover_control', handles, mapping, target_pid=pid + 1)
        checks['wrong_pid_identity_terminal'] = not responses['wrong_pid'].get('success') and responses['wrong_pid'].get('error_kind') == 'identity'
        responses['wrong_root'] = helper(helper_path, dll, 0, pid, 'recover_control', handles, mapping)
        checks['wrong_root_identity_terminal'] = not responses['wrong_root'].get('success') and responses['wrong_root'].get('error_kind') == 'identity'
        command('missing')
        command('recreate')
        responses['restored'] = call('recover_control', handles, mapping)
        checks['fixture_restored'] = responses['restored'].get('success') and str(responses['restored'].get('state')).upper() == 'AUTO'
        command('machine_s3')
        responses['wrong_machine'] = call('recover_control', responses['restored'].get('handles'), responses['restored'].get('field_map'))
        checks['wrong_machine_identity_terminal'] = not responses['wrong_machine'].get('success') and responses['wrong_machine'].get('error_kind') == 'identity'
        command('machine_m1')
        heartbeat = Path(str(marker) + '.heartbeat')
        first = heartbeat.read_text()
        time.sleep(.6)
        second = heartbeat.read_text()
        checks['heartbeat_responsive'] = first.split(',')[0] != second.split(',')[0]
        checks['zero_save_and_navigation'] = second.split(',')[-2:] == ['0', '0']
        poison = Path(str(marker) + '.poison')
        checks['process_panel_not_probed'] = not poison.exists() or not poison.read_text()
        checks['actual_default_transport_used'] = all(r.get('production_adapter_transport') for r in responses.values())
        report['fixture_heartbeat_final'] = second
        report['passed'] = all(checks.values())
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill(); proc.wait(timeout=5)
        proc = None
        report['full_profiles'] = {machine: full_profile(executable, folder, machine) for machine in ('M1', 'S3')}
        checks['full_m1_default_transport_and_audit'] = report['full_profiles']['M1']['passed']
        checks['full_s3_default_transport_and_audit'] = report['full_profiles']['S3']['passed']
        report['passed'] = all(checks.values())
    except Exception as exc:
        report['error'] = str(exc)
        report['traceback'] = traceback.format_exc()
    finally:
        if proc is not None:
            proc.terminate()
            try: proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill(); proc.wait(timeout=5)
        if tmp is not None:
            tmp.cleanup()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    raise SystemExit(main(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()))
