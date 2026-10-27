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

function Owned($root, $window, $pidExpected) {
    return $window -ne [IntPtr]::Zero -and [BinderJetQueuedNavigation]::IsOwned($root, $window, $pidExpected)
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
        if (-not (Owned $root $machineHandle $pidExpected)) { throw 'The cached machine label identity changed.' }
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
    if (-not (Owned $root $stateHandle $pidExpected)) { throw 'A verified task-state label is required before an IR edit or Save.' }
    $state = ([BinderJetNativeProcess]::ReadText($root, $stateHandle, $pidExpected)).Trim()
    if ($state -notin @('AUTO','RUNNING')) { throw 'Automatic recipe updates require a fresh AUTO/RUNNING state.' }
    if ([BinderJetQueuedNavigation]::IsUserEditing($root, $pidExpected)) { throw 'The user is editing a target field; automatic Save is suspended.' }
}

function ClickNavigation($root, $pidExpected, $handles, [string]$name) {
    $key = if ($name -eq 'btnSequences') { 'sequences' } else { throw 'Unsupported navigation.' }
    $button = HandleOf $handles $key
    if ($button -eq [IntPtr]::Zero) { $button = [BinderJetNativeProcess]::FindNavigation($root, $pidExpected, $name) }
    if ([BinderJetQueuedNavigation]::IsUserEditing($root, $pidExpected)) { throw 'The user is editing a target field; automatic page navigation is suspended.' }
    [BinderJetQueuedNavigation]::Click($root, $button, $pidExpected)
    Start-Sleep -Milliseconds 100
    if (-not [BinderJetQueuedNavigation]::Responsive($root, $pidExpected, 250)) { throw 'The target did not acknowledge queued page navigation.' }
}

function FreshLogHint([string]$machine) {
    # Coordinates only, never temperature. Recheck inside this native-only
    # helper immediately before edit/Save: a layer can advance after Python's
    # preflight while the previous temperature remains valid in the row cache.
    if ($machine -notin @('M1','S3') -or [string]::IsNullOrWhiteSpace([string]$request.image_path)) { return $null }
    $stream = $null
    try {
        $directory = [IO.Path]::Combine([IO.Path]::GetDirectoryName([string]$request.image_path), 'Logs')
        if (-not [IO.Directory]::Exists($directory)) { return $null }
        $selected = $null; $count = 0
        foreach ($path in [IO.Directory]::EnumerateFiles($directory, ('*_' + $machine + '.log'))) {
            if (++$count -gt 4096) { break }
            $file = New-Object IO.FileInfo($path)
            if ($null -eq $selected -or $file.LastWriteTimeUtc -gt $selected.LastWriteTimeUtc) { $selected = $file }
        }
        if ($null -eq $selected) { return $null }
        $age = ([DateTime]::UtcNow - $selected.LastWriteTimeUtc).TotalSeconds
        if ($age -lt -5 -or $age -gt 15) { return $null }
        $stream = [IO.File]::Open($selected.FullName, [IO.FileMode]::Open, [IO.FileAccess]::Read, ([IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete))
        [void]$stream.Seek([Math]::Max(0, $stream.Length - 262144), [IO.SeekOrigin]::Begin)
        $buffer = New-Object byte[] 262144; $read = 0
        while ($read -lt $buffer.Length) {
            $part = $stream.Read($buffer, $read, $buffer.Length - $read)
            if ($part -eq 0) { break }; $read += $part
        }
        $hint = $null
        foreach ($line in ([Text.Encoding]::UTF8.GetString($buffer, 0, $read) -split "`n")) {
            if ($line -notmatch 'AUTO') { continue }
            if ($line.Contains('AutoPrint started!')) { $hint = $null }
            if ($line -match '\bLayer:\s*(\d+)\s*->\s*Current Z\s*=') { $hint = [int]$matches[1] }
            if ($line.Contains('AutoPrint finished!') -or $line.Contains('AutoPrint cancelled.') -or $line.Contains('Stopping printing as requested.')) { $hint = $null }
        }
        return $hint
    } catch { return $null }
    finally { if ($null -ne $stream) { $stream.Dispose() } }
}

function FiniteNumber([string]$text) {
    $number = 0.0
    if (-not [double]::TryParse($text.Replace(',','.'), [Globalization.NumberStyles]::Float,
        [Globalization.CultureInfo]::InvariantCulture, [ref]$number) -or [double]::IsNaN($number) -or [double]::IsInfinity($number)) {
        throw 'A finite numeric IR/temperature value is required.'
    }
    return $number
}

function CsvContentHash([string]$path, [long]$expectedSize) {
    if ($expectedSize -le 0 -or $expectedSize -gt 33554432) { throw 'Monitoring CSV content verification exceeds its bounded size.' }
    $stream = $null; $sha = $null
    try {
        $stream = [IO.File]::Open($path, [IO.FileMode]::Open, [IO.FileAccess]::Read, ([IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete))
        if ($stream.Length -ne $expectedSize) { throw 'Monitoring CSV size changed before content verification.' }
        $sha = [Security.Cryptography.SHA256]::Create()
        $buffer = New-Object byte[] 65536
        [long]$count = 0
        while ($count -le $expectedSize) {
            $part = $stream.Read($buffer, 0, [int][Math]::Min($buffer.Length, $expectedSize + 1 - $count))
            if ($part -eq 0) { break }
            $count += $part
            if ($count -gt $expectedSize) { throw 'Monitoring CSV grew during content verification.' }
            [void]$sha.TransformBlock($buffer, 0, $part, $buffer, 0)
        }
        if ($count -ne $expectedSize -or $stream.Length -ne $expectedSize) { throw 'Monitoring CSV changed during content verification.' }
        [void]$sha.TransformFinalBlock([byte[]]@(), 0, 0)
        return ([BitConverter]::ToString($sha.Hash)).Replace('-', '').ToLowerInvariant()
    } finally {
        if ($null -ne $sha) { $sha.Dispose() }
        if ($null -ne $stream) { $stream.Dispose() }
    }
}

function AssertCsvFile($token, $observation) {
    $path = [string](ValueOf $token 'path')
    if ([string]::IsNullOrWhiteSpace($path) -or [IO.Path]::GetFileName($path) -notmatch '^LayerDatasheet_\d{8}_\d{6}\.csv$') { throw 'A verified Monitoring CSV token is required.' }
    $expectedHash = [string](ValueOf $token 'sha256')
    if ($expectedHash -cnotmatch '^[0-9a-f]{64}$') { throw 'A SHA256-bound Monitoring CSV token is required.' }
    $expectedSize = [long](ValueOf $token 'size')
    if ($expectedSize -le 0 -or $expectedSize -gt 33554432) { throw 'Monitoring CSV exceeds the content verification bound.' }
    $file = New-Object IO.FileInfo($path)
    $file.Refresh()
    if (-not $file.Exists -or $file.Length -ne $expectedSize -or
        $file.LastWriteTimeUtc.Ticks -ne [long](ValueOf $token 'mtime_ticks')) {
        throw 'The Monitoring CSV changed after the decision; no settings were saved.'
    }
    # Reopen for the second pass so an atomic replacement cannot leave both
    # checks reading an old, still-open handle. This is bounded evidence,
    # not an atomic lock with the later device operation.
    for ($pass = 0; $pass -lt 2; $pass++) {
        if ((CsvContentHash $path $expectedSize) -cne $expectedHash) { throw 'The Monitoring CSV content changed after the decision.' }
        $file.Refresh()
        if (-not $file.Exists -or $file.Length -ne $expectedSize -or
            $file.LastWriteTimeUtc.Ticks -ne [long](ValueOf $token 'mtime_ticks')) { throw 'The Monitoring CSV changed during verification.' }
    }
    $age = ([DateTime]::UtcNow - $file.LastWriteTimeUtc).TotalSeconds
    if ($age -lt -2 -or $age -gt 5) { throw 'The completed CSV record is no longer fresh enough for an IR update.' }
    $index = [int]$observation.row_index
    $hint = FreshLogHint ([string]$request.machine)
    if ($null -ne $hint -and ($hint -lt $index -or $hint -gt ($index + 1))) { throw 'The production layer advanced beyond the completed CSV decision.' }
}

function UniqueVisibleRecord($records, [string]$id, [bool]$required) {
    $found = @($records | Where-Object { $_.visible -and [string]$_.id -ceq $id })
    if ($found.Count -gt 1 -or ($required -and $found.Count -ne 1)) { throw "A unique visible control '$id' is required." }
    if ($found.Count -eq 1) { return $found[0] }
    return $null
}

function UniqueRecord($records, [string]$id) {
    $found = @($records | Where-Object { [string]$_.id -ceq $id })
    if ($found.Count -gt 1) { throw "Control '$id' is ambiguous." }
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
        $instance = $null
        try {
            $registry = [IO.Path]::Combine([Environment]::GetFolderPath('LocalApplicationData'), 'BinderJet.Unified', 'runtime-instance.json')
            $info = New-Object IO.FileInfo($registry)
            if ($info.Exists -and $info.Length -le 65536) { $instance = [IO.File]::ReadAllText($registry) | ConvertFrom-Json }
        } catch { }
        foreach ($process in Get-Process) {
            $title = [string]$process.MainWindowTitle; $name = [string]$process.ProcessName
            if (($title + ' ' + $name) -notmatch '(?i)Binder\s*Jet|BinderJet|BinderjetController') { continue }
            $window = $process.MainWindowHandle
            if ($window -eq [IntPtr]::Zero) { $window = [BinderJetNativeProcess]::FindRoot([int]$process.Id) }
            if ($window -eq [IntPtr]::Zero) { continue }
            $image = ''; try { $image = [string]$process.Path } catch { }
            $machine = 'unknown'
            if ($null -ne $instance -and [int]$instance.processId -eq $process.Id -and [string]$instance.machineName -in @('M1','S3')) { $machine = [string]$instance.machineName }
            if ($machine -eq 'unknown') { $machine = MachineKind ($title + ' ' + [IO.Path]::GetFileName($image)) }
            if ($machine -eq 'unknown' -and [BinderJetQueuedNavigation]::Responsive($window,[int]$process.Id,100)) {
                try { $machine = [BinderJetNativeProcess]::IdentifyMachine($window, [int]$process.Id) } catch { }
            }
            $windows.Add(@{machine=$machine;pid=$process.Id;hwnd=$window.ToInt64();title=$title;image_path=$image})
            if ($windows.Count -ge 12) { break }
        }
        $response = @{success=$true;windows=@($windows.ToArray())}
    } else {
        $root = [IntPtr][long]$request.hwnd; $pidExpected = [int]$request.pid; $handles = $request.handles
        # Identity validation does not enter the target window procedure.
        [BinderJetNativeProcess]::ValidateWindow($root, $pidExpected)
        $machine = if ($request.operation -eq 'validate') { [string]$request.machine } else { CheckMachine $root $pidExpected $handles }
        switch ([string]$request.operation) {
            'validate' {
                $response = @{success=$true;machine=$machine;handles=@{}}
            }
            'read_status' {
                $stateHandle = HandleOf $handles 'state'
                if (-not (Owned $root $stateHandle $pidExpected)) { throw 'The audited task-state label is unavailable.' }
                $state = ([BinderJetNativeProcess]::ReadText($root, $stateHandle, $pidExpected)).Trim()
                $response = @{success=$true;machine=$machine;state=$state}
            }
            'enter_sequence' {
                # A native menu click creates/shows the existing Sequence view.
                # Never identify, read, scroll or open the Process table.
                ClickNavigation $root $pidExpected $handles 'btnSequences'
                $response = @{success=$true;machine=$machine;entered=$true}
            }
            'metadata' {
                $saveVisible = $false
                foreach ($control in @([BinderJetNativeProcess]::Enumerate($root, $pidExpected))) {
                    if ($control.visible -and [string]$control.class_name -match '(?i)BUTTON' -and
                        ([string]$control.text).Trim() -ieq 'Save') { $saveVisible = $true }
                }
                if (-not $saveVisible) { throw 'The Sequence Save button is not visible; leaf metadata was not collected.' }
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
                foreach ($pair in @(@('machine','labelMachineName'),@('state','labelTaskStatus'),@('sequences','btnSequences'))) {
                    $record = UniqueVisibleRecord $records $pair[1] $true
                    if ($null -ne $record) { $newHandles[$pair[0]] = [long]$record.hwnd }
                }
                $verifiedMachine = NativeMachine $root $pidExpected $newHandles
                if ($verifiedMachine -notin @('M1','S3') -or ($request.machine -and $verifiedMachine -cne [string]$request.machine)) { throw 'Control requires a verified exact M1/S3 native machine label.' }
                $response = @{success=$true;machine=$verifiedMachine;fields=$fields;field_map=$map;handles=$newHandles;setting=$fields['textBoxHeatingOutput']}
            }
            'verify_control' {
                AssertAutomatic $root $pidExpected $handles
                AssertCsvFile $request.csv_token $request.observation
                $scope = HandleOf $handles 'scope'
                if (-not (Owned $root $scope $pidExpected)) { throw 'The audited Sequence view identity changed.' }
                $fields = ReadFields $root $pidExpected $request.field_map $scope
                $response = @{success=$true;fields=$fields;setting=$fields['textBoxHeatingOutput'];handles=$handles;field_map=$request.field_map}
            }
            'apply' {
                AssertAutomatic $root $pidExpected $handles
                $scope = HandleOf $handles 'scope'; $ir = HandleOf $handles 'ir'; $save = HandleOf $handles 'save'
                if (-not (Owned $root $scope $pidExpected)) { throw 'The controlled Sequence view identity changed.' }
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
                AssertCsvFile $request.csv_token $request.observation
                [IO.File]::WriteAllText([string]$request.edit_marker, 'edit-may-have-started')
                [BinderJetNativeFields]::SetIr($root, $ir, $pidExpected, [string]$request.power)
                $after = ReadFields $root $pidExpected $request.field_map $scope
                AssertOtherFields $after $request.baseline
                if ([Math]::Abs((FiniteNumber ([string]$after['textBoxHeatingOutput'])) - $desired) -gt 0.000000001) { throw 'IR input readback failed before Save.' }
                AssertAutomatic $root $pidExpected $handles
                AssertCsvFile $request.csv_token $request.observation
                [BinderJetNativeFields]::ValidateSave($root, $save, $pidExpected)
                # This durable marker precedes the only queued Save. A timeout,
                # cancellation or uncertain result must never trigger a retry.
                [IO.File]::WriteAllText([string]$request.save_marker, 'save-may-have-started')
                [BinderJetNativeFields]::QueueSave($root, $save, $pidExpected)
                Start-Sleep -Milliseconds 100
                if (-not [BinderJetQueuedNavigation]::Responsive($root, $pidExpected, 250)) { throw 'Save was queued but the target acknowledgement is uncertain.' }
                $verified = [BinderJetNativeFields]::Read($root, $ir, $pidExpected, 'text')
                if ([Math]::Abs((FiniteNumber $verified) - $desired) -gt 0.000000001) { throw 'IR readback after queued Save did not match.' }
                $response = @{success=$true;save_invoked=$true;setting=$verified}
            }
            default { throw 'Unsupported native helper operation.' }
        }
    }
    $response | ConvertTo-Json -Depth 10 -Compress
} catch {
    @{success=$false;error=$_.Exception.Message} | ConvertTo-Json -Depth 3 -Compress
    exit 1
}
