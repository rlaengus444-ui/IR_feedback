"""Native hidden-Sequence probe, against owned synthetic fixture only.

python release_tools/windows_hidden_recovery_probe.py PACKAGE_ROOT OUTPUT_JSON [OLD_PACKAGE_ROOT]
Uses production adapter transport and deadlines. No device access, navigation,
IR edit or Save; only the isolated fixture mutates its synthetic controls.
"""
import hashlib
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import traceback

import windows_settings_recovery_probe as fixture_tools

FLAGS = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


def full_hidden_profile(executable, folder, machine, hide_action):
    """Two actual adapter confirmations with full synthetic saved-field audit."""
    from adapter_contracts import MachineWindow
    from windows_ui_adapter import WindowsUiAdapter
    from sequence_field_audit import field_bindings
    import sequence_persistence
    import threading
    bindings = field_bindings(machine)
    spec = folder / ('hidden-' + machine + '-' + hide_action + '.fields')
    spec.write_text('\n'.join(name + '\t' + kind for name, (_, _, kind) in bindings.items()))
    owner = folder / 'Machines' / machine / 'Configs' / 'BJT.parameters.json'
    owner.parent.mkdir(parents=True, exist_ok=True)
    entries = [{'Scope':scope, 'Name':name, 'Type':'BoolParameter' if kind == 'toggle' else 'DoubleParameter',
                'Value':'62' if field == 'textBoxHeatingOutput' else 'False' if kind == 'toggle' else '0'}
               for field, (scope,name,kind) in bindings.items()]
    owner.write_text(json.dumps(entries), encoding='utf-8')
    sha = hashlib.sha256(owner.read_bytes()).hexdigest()
    marker = folder / ('hidden-full-' + machine + '-' + hide_action)
    process = subprocess.Popen([str(executable), str(marker), str(spec), machine], creationflags=FLAGS)
    adapter = WindowsUiAdapter(source_factory=lambda target: None)
    result = {'machine':machine, 'hide_action':hide_action, 'passed':False}
    try:
        def stage(label):
            temporary = Path(str(marker) + '.stage.tmp')
            temporary.write_text(label, encoding='utf-8')
            os.replace(temporary, Path(str(marker) + '.stage'))
        ready = Path(str(marker) + '.ready')
        deadline = time.monotonic() + 20
        while not ready.exists() and time.monotonic() < deadline:
            if process.poll() is not None: raise RuntimeError('Full-profile fixture exited')
            time.sleep(.05)
        if not ready.exists(): raise TimeoutError('Full-profile fixture not ready')
        pid, hwnd = map(int, ready.read_text().split(','))
        if pid != process.pid: raise RuntimeError('Full-profile PID mismatch')
        target = MachineWindow(machine, pid, hwnd, 'Synthetic full profile', str(executable))
        stop = threading.Event()
        adapter.connect(target, stop)
        common = {'pid':pid, 'hwnd':hwnd, 'machine':machine}
        stage('initial-metadata')
        meta = adapter._request(dict(common, operation='metadata'), stop)
        stage('initial-prepare')
        prepared = adapter._request(dict(common, operation='prepare_control', metadata=meta['records']), stop)
        if not prepared.get('handles', {}).get('scope_proof'): raise RuntimeError('No authorized scope proof')
        adapter._mode = 'control'
        adapter._native_handles = prepared['handles']
        adapter._native_fields = prepared['field_map']
        adapter._control_baseline = prepared['fields']
        adapter._persisted_baseline = sequence_persistence.capture(target)
        adapter._expected_ir = str(prepared['setting'])
        def command(action):
            stage('fixture-' + action)
            done = Path(str(marker) + '.done'); done.unlink(missing_ok=True)
            destination = Path(str(marker) + '.command'); temporary = Path(str(marker) + '.command.tmp')
            temporary.write_text(action); os.replace(temporary, destination)
            until = time.monotonic() + 5
            while not done.exists() and time.monotonic() < until: time.sleep(.05)
            if not done.exists() or done.read_text() != action: raise TimeoutError(action)
        command(hide_action)
        result['hidden_window_styles'] = Path(str(marker) + '.styles').read_text()
        adapter.suspend_control('fixture changed')
        command('hide_inner')
        result['explicit_inner_hidden_window_styles'] = Path(str(marker) + '.styles').read_text()
        stage('hidden-inner-recover')
        adapter._recover_control(target, adapter._generation, stop)
        hidden_inner_held = adapter._control_hold_reason is not None and adapter._control_epoch == 0
        command('show_inner')
        time.sleep(1.05)
        command('blank_in_place')
        stage('unnamed-input-recover')
        adapter._recover_control(target, adapter._generation, stop)
        unnamed_reason = adapter._control_hold_reason
        unnamed_held = (unnamed_reason is not None and '저장 매핑이 확인되지 않은 UI 필드' in unnamed_reason
                        and adapter._control_epoch == 0)
        command('remove_extra')
        time.sleep(1.05)
        durations, epochs = [], []
        for attempt in range(2):
            stage('stable-recover-' + str(attempt + 1))
            started = time.monotonic()
            adapter._recover_control(target, adapter._generation, stop)
            durations.append(time.monotonic() - started)
            epochs.append(adapter._control_epoch)
            if attempt == 0: time.sleep(1.05)
        heartbeat = Path(str(marker) + '.heartbeat').read_text().split(',')
        poison = Path(str(marker) + '.poison')
        result.update(epochs=epochs, recovery_seconds=durations, hidden_inner_held=hidden_inner_held,
                      unnamed_input_held=unnamed_held, unnamed_input_reason=unnamed_reason,
                      field_count=len(adapter._native_fields),
                      settings_unchanged=hashlib.sha256(owner.read_bytes()).hexdigest() == sha,
                      zero_save_and_navigation=heartbeat[-2:] == ['0','0'],
                      process_not_probed=not poison.exists() or not poison.read_text(),
                      poison_messages=poison.read_text().splitlines() if poison.exists() else [],
                      hold_reason=adapter._control_hold_reason)
        result['passed'] = (hidden_inner_held and unnamed_held and epochs == [0,1] and adapter._control_hold_reason is None and
            len(adapter._native_fields) == len(bindings) and result['settings_unchanged'] and
            result['zero_save_and_navigation'] and result['process_not_probed'])
    except Exception as error:
        result['error'] = str(error)
    finally:
        adapter.disconnect()
        process.terminate()
        try: process.wait(timeout=5)
        except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=5)
        poison = Path(str(marker) + '.poison')
        result['poison_messages'] = poison.read_text().splitlines() if poison.exists() else []
        result['process_not_probed'] = not result['poison_messages']
        if result['poison_messages']:
            result['passed'] = False
    return result


def run(root, output, old_root=None):
    report = {'passed': False, 'checks': {}, 'responses': {}, 'no_equipment_writes': True,
              'scope': 'Owned synthetic WinForms fixture; recovery does not navigate, edit or Save.',
              'untested': ['Real equipment, actual production BJT UI, Excel COM.',
                           'A truly blank UIA AutomationId: WinForms unnamed TextBox supplies a numeric AutomationId.']}
    processes, tmp, marker = [], None, None
    try:
        if os.name != 'nt':
            raise RuntimeError('Windows required')
        sys.path.insert(0, str(root / 'app'))
        from windows_ui_adapter import WindowsUiAdapter
        from adapter_contracts import AdapterError, ControlSuspended, TargetIdentityError
        import threading
        adapter = WindowsUiAdapter()
        report['default_helper_timeout_seconds'] = adapter.helper_timeout
        dll, helper = root / 'app/UiaBridge.dll', root / 'app/native_helper.ps1'
        report['bridge_sha256'] = hashlib.sha256(dll.read_bytes()).hexdigest()
        report['helper_sha256'] = hashlib.sha256(helper.read_bytes()).hexdigest()
        compiler = Path(os.environ['WINDIR']) / 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
        tmp = tempfile.TemporaryDirectory(prefix='hidden-sequence-probe-')
        folder = Path(tmp.name)
        executable = folder / 'SettingsRecoveryFixture.exe'
        subprocess.run([str(compiler), '/nologo', '/target:winexe', '/platform:x64', '/out:' + str(executable),
                        '/reference:System.Windows.Forms.dll', '/reference:System.Drawing.dll', str(fixture_tools.SOURCE)],
                       check=True, capture_output=True, timeout=30)
        marker = folder / 'fixture'
        report['probe_stages'] = []
        stage_count = [0]
        def stage(label):
            stage_count[0] += 1
            text = str(stage_count[0]) + ':' + label
            temporary = Path(str(marker) + '.stage.tmp')
            temporary.write_text(text, encoding='utf-8')
            os.replace(temporary, Path(str(marker) + '.stage'))
            report['probe_stages'].append({'stage': text, 'utc': datetime.now(timezone.utc).isoformat()})
        stage('fixture-start')
        process = subprocess.Popen([str(executable), str(marker)], creationflags=FLAGS)
        processes.append(process)
        ready = Path(str(marker) + '.ready')
        end = time.monotonic() + 20
        while not ready.exists() and time.monotonic() < end:
            if process.poll() is not None: raise RuntimeError('Fixture exited early')
            time.sleep(.05)
        if not ready.exists(): raise TimeoutError('Fixture did not become ready')
        pid, hwnd = map(int, ready.read_text().split(','))
        if pid != process.pid: raise RuntimeError('Fixture PID mismatch')
        def command(action):
            stage('fixture-' + action)
            done = Path(str(marker) + '.done')
            done.unlink(missing_ok=True)
            path = Path(str(marker) + '.command')
            pending = Path(str(marker) + '.command.tmp')
            pending.write_text(action)
            os.replace(pending, path)
            deadline = time.monotonic() + 5
            while not done.exists() and time.monotonic() < deadline: time.sleep(.05)
            if not done.exists() or done.read_text() != action: raise TimeoutError(action)
        def request(operation, handles=None, mapping=None, **kwargs):
            stage('helper-' + operation)
            payload = {'operation':operation, 'pid':pid, 'hwnd':hwnd, 'machine':'M1',
                       'handles':handles or {}, 'field_map':mapping or {}}
            payload.update(kwargs)
            started = time.monotonic()
            try:
                value = adapter._request(payload, threading.Event())
            except AdapterError as error:
                value = {'success':False, 'error':str(error), 'error_kind':
                         'control_wait' if isinstance(error, ControlSuspended) else
                         'identity' if isinstance(error, TargetIdentityError) else 'operation'}
            value['transport_seconds'] = time.monotonic() - started
            return value
        def styles():
            result = {}
            for line in Path(str(marker) + '.styles').read_text().splitlines():
                name, values = line.split('=', 1)
                raw, effective = values.split(',')
                style = int(raw)
                result[name] = {'style': style, 'ws_visible': bool(style & 0x10000000),
                                'effective_visible': effective == 'True'}
            return result
        checks, responses = report['checks'], report['responses']
        report['native_window_styles'] = {'initial_visible': styles()}
        metadata = request('metadata')
        initial = request('prepare_control', metadata=metadata.get('records', []))
        responses['initial'] = initial
        if not initial.get('success'): raise RuntimeError('Initial visible authorization failed: ' + str(initial))
        handles, mapping = initial['handles'], initial['field_map']
        checks['initial_scope_proof'] = bool(handles.get('scope_proof'))
        # The valid visible Sequence would make broad root re-audit succeed.
        # Forging the scope as the root must instead fail closed immediately.
        forged = dict(handles)
        forged['scope'] = hwnd
        forged['scope_proof'] = dict(handles.get('scope_proof') or {})
        forged['scope_proof']['scope_hwnd'] = hwnd
        responses['forged_root_scope'] = request('recover_control', forged, mapping)
        checks['forged_root_scope_holds'] = (not responses['forged_root_scope'].get('success') and
                                              responses['forged_root_scope'].get('error_kind') == 'control_wait')
        command('focus')
        responses['editing'] = request('recover_control', handles, mapping)
        checks['focused_edit_holds'] = not responses['editing'].get('success') and responses['editing'].get('error_kind') == 'control_wait'
        command('clear')
        if old_root is not None:
            # Separate interpreter ensures old adapter actually loads old helper/DLL bytes.
            old_code = '''import json,sys,threading\nsys.path.insert(0,sys.argv[1]+"/app")\nfrom windows_ui_adapter import WindowsUiAdapter\na=WindowsUiAdapter()\nr=json.loads(sys.stdin.read())\ntry:\n v=a._request(r,threading.Event())\nexcept Exception as e:\n v={"success":False,"error":str(e)}\nprint(json.dumps(v))'''
            def old_call(operation, handles=None, mapping=None, **extra):
                stage('old-helper-' + operation)
                payload = {'operation':operation, 'pid':pid, 'hwnd':hwnd, 'machine':'M1', 'handles':handles or {}, 'field_map':mapping or {}}
                payload.update(extra)
                result = subprocess.run([sys.executable, '-c', old_code, str(old_root)], input=json.dumps(payload),
                                        capture_output=True, text=True, timeout=12, creationflags=FLAGS)
                return json.loads(result.stdout.strip())
            old_meta = old_call('metadata')
            old_prepared = old_call('prepare_control', metadata=old_meta.get('records', []))
            if not old_prepared.get('success'): raise RuntimeError('Old visible fixture prepare failed: ' + str(old_prepared))
        command('hide_outer')
        report['native_window_styles']['native_hidden_outer'] = styles()
        native_styles = report['native_window_styles']['native_hidden_outer']
        checks['native_hide_style_evidence'] = (not native_styles['outer']['ws_visible'] and
            all(native_styles[name]['ws_visible'] and not native_styles[name]['effective_visible'] for name in ('scope','ir','save')))
        if old_root is not None:
            responses['old_hidden'] = old_call('recover_control', old_prepared['handles'], old_prepared['field_map'])
            checks['old_hidden_failure_reproduced'] = not responses['old_hidden'].get('success')
        else:
            report['untested'].append('Old release reproduction omitted: OLD_PACKAGE_ROOT was not supplied.')
        responses['hidden'] = request('recover_control', handles, mapping)
        checks['hidden_scope_recovery'] = responses['hidden'].get('success') and str(responses['hidden'].get('setting')) == '62'
        command('show_outer')
        command('hide_outer_managed')
        managed_styles = styles()
        report['native_window_styles']['winforms_property_hidden_outer'] = managed_styles
        checks['winforms_hide_style_evidence'] = (not managed_styles['outer']['ws_visible'] and
            all(managed_styles[name]['ws_visible'] and not managed_styles[name]['effective_visible'] for name in ('scope','ir','save')))
        responses['winforms_hidden'] = request('recover_control', handles, mapping)
        checks['winforms_hidden_scope_recovery'] = responses['winforms_hidden'].get('success') and str(responses['winforms_hidden'].get('setting')) == '62'
        unproven = dict(handles); unproven.pop('scope_proof', None)
        responses['hidden_unproven'] = request('recover_control', unproven, mapping)
        checks['hidden_unproven_holds'] = not responses['hidden_unproven'].get('success')
        wrong = dict(handles); wrong['machine'] = wrong.get('state')
        responses['wrong_anchor'] = request('recover_control', wrong, mapping)
        checks['global_anchor_invalid_rejected'] = not responses['wrong_anchor'].get('success')
        foreground = subprocess.Popen([str(executable), '--foreground'], creationflags=FLAGS)
        processes.append(foreground)
        time.sleep(.6)
        responses['other_foreground'] = request('recover_control', handles, mapping)
        checks['other_foreground_recovery'] = responses['other_foreground'].get('success')
        command('minimize')
        responses['minimized'] = request('recover_control', handles, mapping)
        checks['minimized_recovery'] = responses['minimized'].get('success')
        command('restore')
        for control in ('ir', 'save', 'inner'):
            report['native_window_styles']['before_explicit_hide_' + control] = styles()
            command('hide_' + control)
            report['native_window_styles']['explicit_hidden_' + control] = styles()
            checks['explicit_hide_' + control + '_style_cleared'] = not report['native_window_styles']['explicit_hidden_' + control][control]['ws_visible']
            response = request('recover_control', handles, mapping)
            responses['hidden_' + control] = response
            checks['explicit_hidden_' + control + '_holds'] = ((not response.get('success') and response.get('error_kind') == 'control_wait') if control != 'inner' else (not response.get('success') or 'textBoxSpeed' not in response.get('fields', {})))
            command('show_' + control)
        command('duplicate_in_place')
        responses['duplicate'] = request('recover_control', handles, mapping)
        checks['duplicate_id_holds'] = not responses['duplicate'].get('success') and responses['duplicate'].get('error_kind') == 'control_wait'
        command('remove_extra')
        command('blank_in_place')
        responses['blank'] = request('recover_control', handles, mapping)
        unnamed_fields = set(responses['blank'].get('field_map', {})) - set(mapping)
        checks['unnamed_input_inventoried'] = (responses['blank'].get('success') and len(unnamed_fields) == 1 and
            all(str(responses['blank'].get('fields', {}).get(field)) == '1' for field in unnamed_fields))
        report['unnamed_fixture_automation_ids'] = sorted(unnamed_fields)
        command('remove_extra')
        command('extra_in_place')
        responses['unknown'] = request('recover_control', handles, mapping)
        checks['unknown_id_in_inventory'] = responses['unknown'].get('success') and 'textBoxUnexpected' in responses['unknown'].get('field_map', {})
        command('remove_extra')
        command('pause')
        responses['paused'] = request('recover_control', handles, mapping)
        checks['paused_holds'] = not responses['paused'].get('success') and responses['paused'].get('error_kind') == 'control_wait'
        command('auto')
        command('missing')
        responses['reparented'] = request('recover_control', handles, mapping)
        checks['reparented_scope_holds'] = not responses['reparented'].get('success')
        heartbeat = Path(str(marker) + '.heartbeat')
        first = heartbeat.read_text(); time.sleep(.5); second = heartbeat.read_text()
        checks['heartbeat_responsive'] = first.split(',')[0] != second.split(',')[0]
        checks['zero_save_or_navigation'] = second.split(',')[-2:] == ['0','0']
        poison = Path(str(marker) + '.poison')
        checks['process_not_probed'] = not poison.exists() or not poison.read_text()
        report['poison_messages'] = poison.read_text().splitlines() if poison.exists() else []
        report['full_profiles'] = {m + '_' + action:full_hidden_profile(executable, folder, m, action)
            for m in ('M1','S3') for action in ('hide_outer','hide_outer_managed')}
        checks['full_m1_s3_both_hide_modes_two_stable_audits'] = all(p['passed'] for p in report['full_profiles'].values())
        report['passed'] = all(checks.values())
    except Exception as exc:
        report['error'], report['traceback'] = str(exc), traceback.format_exc()
    finally:
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
                try: process.wait(timeout=5)
                except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=5)
        if marker is not None:
            poison = Path(str(marker) + '.poison')
            report['poison_messages'] = poison.read_text().splitlines() if poison.exists() else []
            report['checks']['process_not_probed'] = not report['poison_messages']
            report['passed'] = bool(report['passed'] and not report['poison_messages'])
        if tmp is not None: tmp.cleanup()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    if len(sys.argv) not in (3,4): raise SystemExit(__doc__)
    raise SystemExit(run(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve(), Path(sys.argv[3]).resolve() if len(sys.argv)==4 else None))
