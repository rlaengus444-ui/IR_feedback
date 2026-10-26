$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = New-Object Text.UTF8Encoding($false)
[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)

function ValueOf($item, [string]$name) {
    if ($null -eq $item) { return $null }
    if ($item -is [Collections.IDictionary]) { return $item[$name] }
    $property = $item.PSObject.Properties[$name]
    if ($null -ne $property) { return $property.Value }
    return $null
}

function EntriesOf($item) {
    if ($null -eq $item) { return }
    if ($item -is [Collections.IDictionary]) {
        foreach ($entry in $item.GetEnumerator()) { [PSCustomObject]@{Name=[string]$entry.Key; Value=$entry.Value} }
    } else {
        foreach ($property in $item.PSObject.Properties) { [PSCustomObject]@{Name=$property.Name; Value=$property.Value} }
    }
}

function HandleOf($handles, [string]$name) {
    $value = ValueOf $handles $name
    if ($null -eq $value) { return [IntPtr]::Zero }
    return [IntPtr][long]$value
}

function Visible($root, $window, $pidExpected) {
    return $window -ne [IntPtr]::Zero -and [BinderJetQueuedNavigation]::IsVisible($root, $window, $pidExpected)
}

function MachineKind([string]$text) {
    $matchesMachine = [regex]::Matches($text, '(?i)(?:^|[^A-Z0-9])(M1|S3)(?:$|[^A-Z0-9])')
    $families = @($matchesMachine | ForEach-Object { $_.Groups[1].Value.ToUpperInvariant() } | Select-Object -Unique)
    if ($families.Count -eq 1) { return [string]$families[0] }
    return 'unknown'
}

function NativeMachine($root, $pidExpected, $handles) {
    $machineHandle = HandleOf $handles 'machine'
    if ($machineHandle -ne [IntPtr]::Zero) {
        if (-not (Visible $root $machineHandle $pidExpected)) { throw 'The cached machine label is no longer visible.' }
        $machine = ([BinderJetNativeProcess]::ReadText($root, $machineHandle, $pidExpected)).Trim().ToUpperInvariant()
        if ($machine -notin @('M1','S3')) { throw 'The cached machine label is no longer exactly M1/S3.' }
        return $machine
    }
    return [BinderJetNativeProcess]::IdentifyMachine($root, $pidExpected)
}

function CheckMachine($root, $pidExpected, $handles) {
    $machine = NativeMachine $root $pidExpected $handles
    if ($request.machine -and $machine -ne 'unknown' -and $machine -cne [string]$request.machine) {
        throw 'The selected native machine identity changed.'
    }
    return $machine
}

function AssertAutomatic($root, $pidExpected, $handles) {
    $stateHandle = HandleOf $handles 'state'
    if (-not (Visible $root $stateHandle $pidExpected)) { throw 'A verified, visible task-state label is required before an IR edit or Save.' }
    $state = ([BinderJetNativeProcess]::ReadText($root, $stateHandle, $pidExpected)).Trim()
    if ($state -notin @('AUTO','RUNNING')) { throw 'Automatic recipe updates require a fresh AUTO/RUNNING state.' }
    if ([BinderJetQueuedNavigation]::IsUserEditing($root, $pidExpected)) { throw 'The user is editing a target field; automatic Save is suspended.' }
}

function ClickNavigation($root, $pidExpected, $handles, [string]$name) {
    $key = if ($name -eq 'btnProcess') { 'process' } elseif ($name -eq 'btnSequences') { 'sequences' } else { throw 'Unsupported navigation.' }
    $button = HandleOf $handles $key
    if ($button -eq [IntPtr]::Zero) { $button = [BinderJetNativeProcess]::FindNavigation($root, $pidExpected, $name) }
    if ([BinderJetQueuedNavigation]::IsUserEditing($root, $pidExpected)) { throw 'The user is editing a target field; automatic page navigation is suspended.' }
    [BinderJetQueuedNavigation]::Click($root, $button, $pidExpected)
    Start-Sleep -Milliseconds 100
    if (-not [BinderJetQueuedNavigation]::Responsive($root, $pidExpected, 250)) { throw 'The target did not acknowledge queued page navigation.' }
}

function NativeState($root, $pidExpected, $providedHandles, $hint, [bool]$follow, [bool]$requireProcess) {
    $handles = @{}
    foreach ($entry in @(EntriesOf $providedHandles)) { $handles[$entry.Name] = $entry.Value }
    $machine = CheckMachine $root $pidExpected $handles
    $state = 'unknown'; $progress = ''
    $stateHandle = HandleOf $handles 'state'; $progressHandle = HandleOf $handles 'progress'
    if ($stateHandle -ne [IntPtr]::Zero) {
        if (-not (Visible $root $stateHandle $pidExpected)) { throw 'The cached task-state label is no longer visible.' }
        $state = ([BinderJetNativeProcess]::ReadText($root, $stateHandle, $pidExpected)).Trim()
    }
    if ($progressHandle -ne [IntPtr]::Zero) {
        if (-not (Visible $root $progressHandle $pidExpected)) { throw 'The cached progress label is no longer visible.' }
        $progress = [BinderJetNativeProcess]::ReadText($root, $progressHandle, $pidExpected)
    }
    # Before leaf IDs have been collected, inspect only native visible STATIC
    # text. Ambiguous labels are rejected instead of adopting another control.
    if ($stateHandle -eq [IntPtr]::Zero -or $progressHandle -eq [IntPtr]::Zero) {
        $stateCandidates = @(); $progressCandidates = @()
        foreach ($control in @([BinderJetNativeProcess]::Enumerate($root, $pidExpected))) {
            if (-not $control.visible -or [string]$control.class_name -notmatch '(?i)STATIC') { continue }
            $text = ([string]$control.text).Trim()
            if ($text -in @('AUTO','RUNNING','IDLE','PAUSED','PAUSE','STOPPED','STOP','ERROR','FAULT','UNKNOWN')) { $stateCandidates += $control }
            if ($text -match '(?i)\bLayer\s*[:#]?\s*\d+\s*/') { $progressCandidates += $control }
        }
        if ($stateHandle -eq [IntPtr]::Zero -and $stateCandidates.Count -eq 1) {
            $state = ([string]$stateCandidates[0].text).Trim(); $handles['state'] = [long]$stateCandidates[0].hwnd
        } elseif ($stateHandle -eq [IntPtr]::Zero -and $stateCandidates.Count -gt 1) { throw 'The native task-state label is ambiguous.' }
        if ($progressHandle -eq [IntPtr]::Zero -and $progressCandidates.Count -eq 1) {
            $progress = [string]$progressCandidates[0].text; $handles['progress'] = [long]$progressCandidates[0].hwnd
        } elseif ($progressHandle -eq [IntPtr]::Zero -and $progressCandidates.Count -gt 1) { throw 'The native layer-progress label is ambiguous.' }
    }
    if ([string]::IsNullOrWhiteSpace($state)) { $state = 'unknown' }
    if ($null -eq $hint -and $progress -match '(?i)\bLayer\s*[:#]?\s*(\d+)\s*/') { $hint = [Math]::Max(0, [int]$matches[1] - 1) }
    $rowHint = if ($null -eq $hint) { 0 } else { [int]$hint }
    if ($rowHint -lt 0 -or $rowHint -gt 1000000) { throw 'The current layer hint is outside its supported bound.' }
    $grid = HandleOf $handles 'grid'
    if ($grid -ne [IntPtr]::Zero) {
        if (-not (Visible $root $grid $pidExpected)) { $grid = [IntPtr]::Zero }
        elseif (-not [BinderJetAccessibleGrid]::HasRecoatingHeaders($grid)) { throw 'The cached native table no longer has the verified T_recoating headers.' }
    } else { $grid = [BinderJetNativeProcess]::FindGrid($root, $pidExpected, $rowHint) }
    $rows = @(); $visibleGrid = $grid -ne [IntPtr]::Zero
    if ($visibleGrid) {
        $handles['grid'] = $grid.ToInt64()
        $rows = @([BinderJetAccessibleGrid]::ReadRows($grid, $rowHint))
        $current = @($rows | Where-Object { [int]$_.row_index -eq $rowHint })
        if ($follow -and $current.Count -eq 1 -and
            ([string]$current[0].start -match '0001' -or [string]$current[0].temperature -eq '0')) {
            [void][BinderJetQueuedGridScroll]::Follow($root, $grid, $pidExpected, $rowHint, [int]$current[0].row_count)
            Start-Sleep -Milliseconds 200
            if (-not [BinderJetQueuedNavigation]::Responsive($root, $pidExpected, 250)) { throw 'The target did not acknowledge bounded native row following.' }
            $rows = @([BinderJetAccessibleGrid]::ReadRows($grid, $rowHint))
        }
    }
    if ($requireProcess -and -not $visibleGrid) { throw 'The controlled Process T_recoating table is no longer visible.' }
    return @{success=$true; machine=$machine; state=$state; setting=$null; status_powers=@{}; progress=$progress;
        layer_label=''; rows=$rows; visible_grid=$visibleGrid; handles=$handles; layer_hint=$hint}
}

function FiniteNumber([string]$text) {
    $number = 0.0
    if (-not [double]::TryParse($text.Replace(',','.'), [Globalization.NumberStyles]::Float,
        [Globalization.CultureInfo]::InvariantCulture, [ref]$number) -or [double]::IsNaN($number) -or [double]::IsInfinity($number)) {
        throw 'A finite numeric IR/temperature value is required.'
    }
    return $number
}

function AssertObservation($snapshot, $expected) {
    if ($snapshot.state -notin @('AUTO','RUNNING')) { throw 'A fresh T_recoating decision requires AUTO/RUNNING.' }
    if ($null -eq $snapshot.layer_hint) { throw 'A verified current production layer is required before an IR decision.' }
    $expectedIndex = [int]$expected.row_index
    if ($expectedIndex -lt ([int]$snapshot.layer_hint - 1) -or $expectedIndex -gt [int]$snapshot.layer_hint) {
        throw 'The expected T_recoating row does not match the current production layer.'
    }
    $found = $false
    foreach ($row in @($snapshot.rows)) {
        if ([string]$row.start -match '0001') { continue }
        try { $temperature = FiniteNumber ([string]$row.temperature) } catch { continue }
        if ($temperature -eq 0) { continue }
        if ([int]$row.row_index -gt $expectedIndex) { throw 'A newer completed T_recoating row exists; the old decision was not saved.' }
        if ([int]$row.row_index -eq $expectedIndex -and [string]$row.start -ceq [string]$expected.start -and
            [Math]::Abs($temperature - (FiniteNumber ([string]$expected.temperature))) -le 0.000000001) { $found = $true }
    }
    if (-not $found) { throw 'The expected fresh T_recoating row is no longer available; no settings were saved.' }
}

function UniqueVisibleRecord($records, [string]$id, [bool]$required) {
    $found = @($records | Where-Object { $_.visible -and [string]$_.id -ceq $id })
    if ($found.Count -gt 1 -or ($required -and $found.Count -ne 1)) { throw "A unique visible control '$id' is required." }
    if ($found.Count -eq 1) { return $found[0] }
    return $null
}

function ReadFields($root, $pidExpected, $fieldMap, $scope = [IntPtr]::Zero) {
    $entries = @(EntriesOf $fieldMap)
    if ($entries.Count -eq 0 -or $entries.Count -gt 256) { throw 'The native Sequence field map is empty or exceeds its bound.' }
    $values = @{}
    foreach ($entry in $entries) {
        if ([string]::IsNullOrWhiteSpace($entry.Name) -or $values.ContainsKey($entry.Name)) { throw 'The native Sequence field map has a blank/duplicate ID.' }
        $window = [IntPtr][long](ValueOf $entry.Value 'hwnd'); $kind = [string](ValueOf $entry.Value 'kind')
        if ($scope -ne [IntPtr]::Zero -and -not [BinderJetLeafMetadata]::IsDescendant($root, $scope, $window, $pidExpected)) { throw 'A native Sequence input left its verified view scope.' }
        if ($kind -notin @('text','toggle') -or [BinderJetNativeFields]::Kind($root, $window, $pidExpected) -cne $kind) { throw 'A native Sequence input no longer has its verified kind.' }
        $values[$entry.Name] = [BinderJetNativeFields]::Read($root, $window, $pidExpected, $kind)
    }
    return $values
}

function AssertOtherFields($actual, $baseline) {
    $before = @{}
    foreach ($entry in @(EntriesOf $baseline)) { if ($entry.Name -cne 'textBoxHeatingOutput') { $before[$entry.Name] = [string]$entry.Value } }
    $now = @{}
    foreach ($entry in @(EntriesOf $actual)) { if ($entry.Name -cne 'textBoxHeatingOutput') { $now[$entry.Name] = [string]$entry.Value } }
    if ($before.Count -eq 0 -or $before.Count -ne $now.Count) { throw 'The Sequence input set changed; no automatic Save is allowed.' }
    foreach ($key in $before.Keys) { if (-not $now.ContainsKey($key) -or [string]$now[$key] -cne [string]$before[$key]) { throw "A non-IR Sequence input changed ('$key'); no automatic Save is allowed." } }
}

try {
    $request = [Console]::In.ReadToEnd() | ConvertFrom-Json
    Add-Type -Path ([string]$request.bridge_path)
    if ($request.operation -eq 'discover') {
        $windows = New-Object 'Collections.Generic.List[object]'
        foreach ($process in Get-Process) {
            if ($process.MainWindowHandle -eq [IntPtr]::Zero) { continue }
            $title = [string]$process.MainWindowTitle; $name = [string]$process.ProcessName
            if (($title + ' ' + $name) -notmatch '(?i)Binder\s*Jet|BinderJet|BinderjetController') { continue }
            $image = ''; try { $image = [string]$process.Path } catch { }
            $machine = 'unknown'
            try {
                $native = [BinderJetNativeProcess]::IdentifyMachine($process.MainWindowHandle, [int]$process.Id)
                if ($native -ne 'unknown') { $machine = $native }
            } catch { }
            if ($machine -eq 'unknown') { $machine = MachineKind ($title + ' ' + [IO.Path]::GetFileName($image)) }
            $windows.Add(@{machine=$machine;pid=$process.Id;hwnd=$process.MainWindowHandle.ToInt64();title=$title;image_path=$image})
            if ($windows.Count -ge 12) { break }
        }
        $response = @{success=$true;windows=@($windows.ToArray())}
    } else {
        $root = [IntPtr][long]$request.hwnd; $pidExpected = [int]$request.pid; $handles = $request.handles
        # ReadText validates HWND/PID even if metadata and all optional handles
        # are absent. This standard scalar/text read never creates a UIA root.
        [void][BinderJetNativeProcess]::ReadText($root, $root, $pidExpected)
        $machine = CheckMachine $root $pidExpected $handles
        switch ([string]$request.operation) {
            { $_ -in @('validate','read') } {
                $response = NativeState $root $pidExpected $handles $request.layer_hint ([bool]$request.follow_row) ([bool]$request.require_process)
            }
            'enter_sequence' {
                $entered = $false
                if ($null -ne $request.observation) {
                    $snapshot = NativeState $root $pidExpected $handles $request.layer_hint $true $true
                    AssertObservation $snapshot $request.observation
                    if ([BinderJetQueuedNavigation]::IsUserEditing($root, $pidExpected)) { throw 'The user is editing a target field; the pending decision was not applied.' }
                    ClickNavigation $root $pidExpected $handles 'btnSequences'
                    $entered = $true
                } else {
                    $hint = if ($null -eq $request.layer_hint) { 0 } else { [int]$request.layer_hint }
                    $grid = [BinderJetNativeProcess]::FindGrid($root, $pidExpected, $hint)
                    if ($grid -ne [IntPtr]::Zero -or [bool]$request.force_sequence) {
                        ClickNavigation $root $pidExpected $handles 'btnSequences'
                        $entered = $true
                    }
                }
                $response = @{success=$true;machine=$machine;entered=$entered}
            }
            'metadata' {
                $hint = if ($null -eq $request.layer_hint) { 0 } else { [int]$request.layer_hint }
                if ([BinderJetNativeProcess]::FindGrid($root, $pidExpected, $hint) -ne [IntPtr]::Zero) { throw 'Leaf metadata collection requires the Process grid to be hidden.' }
                $response = @{success=$true;records=@([BinderJetLeafMetadata]::Collect($root, $pidExpected))}
            }
            'prepare_control' {
                $records = @($request.metadata | Where-Object { $null -ne $_ })
                if ($records.Count -eq 0) { $records = @($request.records | Where-Object { $null -ne $_ }) }
                if ($records.Count -eq 0 -or $records.Count -gt 900) { throw 'A bounded native leaf metadata snapshot is required.' }
                $irRecord = UniqueVisibleRecord $records 'textBoxHeatingOutput' $true
                $saveRecord = UniqueVisibleRecord $records 'btnSave' $true
                $ir = [IntPtr][long]$irRecord.hwnd; $save = [IntPtr][long]$saveRecord.hwnd
                $scope = [BinderJetLeafMetadata]::CommonAncestor($root, $ir, $save, $pidExpected)
                if ($scope -eq $root) { throw 'IR and Save do not belong to an isolated Sequence view.' }
                $map = @{}
                foreach ($record in $records) {
                    if (-not $record.visible) { continue }
                    $window = [IntPtr][long]$record.hwnd
                    if (-not [BinderJetLeafMetadata]::IsDescendant($root, $scope, $window, $pidExpected)) { continue }
                    $kind = [BinderJetNativeFields]::Kind($root, $window, $pidExpected)
                    if ($kind -notin @('text','toggle')) { continue }
                    $id = [string]$record.id
                    if ([string]::IsNullOrWhiteSpace($id) -or $map.ContainsKey($id)) { throw 'A visible Sequence input has a blank or duplicate AutomationId.' }
                    $map[$id] = @{hwnd=$window.ToInt64();kind=$kind}
                }
                if (-not $map.ContainsKey('textBoxHeatingOutput')) { throw 'The native Sequence IR field is missing.' }
                [BinderJetNativeFields]::ValidateSave($root, $save, $pidExpected)
                $fields = ReadFields $root $pidExpected $map $scope
                $newHandles = @{scope=$scope.ToInt64();save=$save.ToInt64();ir=$ir.ToInt64()}
                foreach ($pair in @(@('machine','labelMachineName'),@('state','labelTaskStatus'),@('progress','lblProgress'),@('process','btnProcess'),@('sequences','btnSequences'))) {
                    $record = UniqueVisibleRecord $records $pair[1] ($pair[0] -in @('machine','state','process','sequences'))
                    if ($null -ne $record) { $newHandles[$pair[0]] = [long]$record.hwnd }
                }
                $verifiedMachine = NativeMachine $root $pidExpected $newHandles
                if ($verifiedMachine -notin @('M1','S3') -or ($request.machine -and $verifiedMachine -cne [string]$request.machine)) { throw 'Control requires a verified exact M1/S3 native machine label.' }
                $response = @{success=$true;machine=$verifiedMachine;fields=$fields;field_map=$map;handles=$newHandles;setting=$fields['textBoxHeatingOutput']}
            }
            'apply' {
                AssertAutomatic $root $pidExpected $handles
                $scope = HandleOf $handles 'scope'; $ir = HandleOf $handles 'ir'; $save = HandleOf $handles 'save'
                if (-not (Visible $root $scope $pidExpected)) { throw 'The controlled Sequence view is no longer visible.' }
                if (-not [BinderJetLeafMetadata]::IsDescendant($root, $scope, $ir, $pidExpected) -or
                    -not [BinderJetLeafMetadata]::IsDescendant($root, $scope, $save, $pidExpected)) { throw 'IR/Save no longer belong to the controlled Sequence view.' }
                $irMap = ValueOf $request.field_map 'textBoxHeatingOutput'
                if ($null -eq $irMap -or [long](ValueOf $irMap 'hwnd') -ne $ir.ToInt64() -or [string](ValueOf $irMap 'kind') -cne 'text') { throw 'The IR field map no longer matches its verified native handle.' }
                $before = ReadFields $root $pidExpected $request.field_map $scope
                AssertOtherFields $before $request.baseline
                if (-not $before.ContainsKey('textBoxHeatingOutput') -or [string]$before['textBoxHeatingOutput'] -cne [string]$request.expected_ir) { throw 'The IR setting changed outside this module; no settings were saved.' }
                $desired = FiniteNumber ([string]$request.power)
                if ($desired -lt 0 -or $desired -gt 100) { throw 'IR recipe power must be in 0..100.' }
                if ([string]::IsNullOrWhiteSpace([string]$request.edit_marker) -or [string]::IsNullOrWhiteSpace([string]$request.save_marker)) { throw 'Write-ahead edit/Save markers are required.' }
                [BinderJetNativeFields]::ValidateSave($root, $save, $pidExpected)
                AssertAutomatic $root $pidExpected $handles
                [IO.File]::WriteAllText([string]$request.edit_marker, 'edit-may-have-started')
                [BinderJetNativeFields]::SetIr($root, $ir, $pidExpected, [string]$request.power)
                $after = ReadFields $root $pidExpected $request.field_map $scope
                AssertOtherFields $after $request.baseline
                if ([Math]::Abs((FiniteNumber ([string]$after['textBoxHeatingOutput'])) - $desired) -gt 0.000000001) { throw 'IR input readback failed before Save.' }
                AssertAutomatic $root $pidExpected $handles
                [BinderJetNativeFields]::ValidateSave($root, $save, $pidExpected)
                # This durable marker precedes the only queued Save. A timeout,
                # cancellation or uncertain result must never trigger a retry.
                [IO.File]::WriteAllText([string]$request.save_marker, 'save-may-have-started')
                [BinderJetQueuedNavigation]::Click($root, $save, $pidExpected)
                Start-Sleep -Milliseconds 100
                if (-not [BinderJetQueuedNavigation]::Responsive($root, $pidExpected, 250)) { throw 'Save was queued but the target acknowledgement is uncertain.' }
                $verified = [BinderJetNativeFields]::Read($root, $ir, $pidExpected, 'text')
                if ([Math]::Abs((FiniteNumber $verified) - $desired) -gt 0.000000001) { throw 'IR readback after queued Save did not match.' }
                $response = @{success=$true;save_invoked=$true;setting=$verified}
            }
            'return_process' {
                $scope = HandleOf $handles 'scope'; $grid = HandleOf $handles 'grid'
                $processVisible = (Visible $root $grid $pidExpected)
                if ($processVisible -and -not [BinderJetAccessibleGrid]::HasRecoatingHeaders($grid)) { throw 'The cached Process table changed.' }
                if (-not $processVisible) {
                    $hint = if ($null -eq $request.layer_hint) { 0 } else { [int]$request.layer_hint }
                    $grid = [BinderJetNativeProcess]::FindGrid($root, $pidExpected, $hint)
                    $processVisible = $grid -ne [IntPtr]::Zero
                }
                if (-not $processVisible) {
                    if (-not (Visible $root $scope $pidExpected)) { throw 'The user changed pages; the module will not navigate over that change.' }
                    ClickNavigation $root $pidExpected $handles 'btnProcess'
                }
                if (-not [BinderJetQueuedNavigation]::Responsive($root, $pidExpected, 250)) { throw 'The Process return heartbeat failed.' }
                $response = @{success=$true}
            }
            default { throw 'Unsupported native helper operation.' }
        }
    }
    $response | ConvertTo-Json -Depth 10 -Compress
} catch {
    @{success=$false;error=$_.Exception.Message} | ConvertTo-Json -Depth 3 -Compress
    exit 1
}
