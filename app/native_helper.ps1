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
    $script:controlWait = $true
    $machine = NativeMachine $root $pidExpected $handles
    if ($request.machine -and $machine -ne 'unknown' -and $machine -cne [string]$request.machine) {
        $script:identityFailure = $true
        throw 'The selected native machine identity changed.'
    }
    $script:controlWait = $false
    return $machine
}

function AssertAutomatic($root, $pidExpected, $handles) {
    $script:controlWait = $true
    $stateHandle = HandleOf $handles 'state'
    if (-not (Owned $root $stateHandle $pidExpected)) { throw 'A verified task-state label is required before an IR edit or Save.' }
    $state = ([BinderJetNativeProcess]::ReadText($root, $stateHandle, $pidExpected)).Trim()
    if ($state -notin @('AUTO','RUNNING')) { throw 'Automatic recipe updates require a fresh AUTO/RUNNING state.' }
    if ([BinderJetQueuedNavigation]::IsUserEditing($root, $pidExpected)) { throw 'The user is editing a target field; automatic Save is suspended.' }
    $script:controlWait = $false
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

function SelectJobProofLog([string]$directory, [string]$machine) {
    $selected=$null; $count=0
    foreach ($candidate in [IO.Directory]::EnumerateFiles($directory, ('*_'+$machine+'.log'))) {
        if (++$count -gt 4096) { StopObservation 'Controller log inventory exceeds its bound.' }
        $file=New-Object IO.FileInfo($candidate)
        if ($null -eq $selected -or $file.LastWriteTimeUtc -gt $selected.LastWriteTimeUtc -or
            ($file.LastWriteTimeUtc -eq $selected.LastWriteTimeUtc -and [string]::CompareOrdinal($file.FullName,$selected.FullName) -gt 0)) { $selected=$file }
    }
    return $selected
}

function AssertJobProof($proof, [int]$index) {
    # The Python reader binds its parsed job context to this exact consumed
    # window. Re-read it and all new bytes here, inside both native apply gates.
    # No wall-clock or same-layer guess can authorize a different run.
    if ($null -eq $proof) { StopObservation 'Current job proof is unavailable; waiting for a verified run.' }
    $path = [string](ValueOf $proof 'path')
    $expectedHash = [string](ValueOf $proof 'sha256')
    $run = [string](ValueOf $proof 'run_key')
    $offset = ValueOf $proof 'offset'; $start = ValueOf $proof 'start'; $layer = ValueOf $proof 'layer'
    if ($expectedHash -cnotmatch '^[0-9a-f]{64}$' -or $run -cnotmatch '^[0-9a-f]{64}$' -or
        ($offset -isnot [int] -and $offset -isnot [long]) -or ($start -isnot [int] -and $start -isnot [long]) -or
        ($layer -isnot [int] -and $layer -isnot [long]) -or $start -lt 0 -or $offset -le $start -or
        ($offset - $start) -gt 8388608 -or $layer -lt 0 -or $layer -gt 1000000) { throw 'Invalid bounded job proof.' }
    $stream = $null; $sha = $null
    try {
        $directory = [IO.Path]::Combine([IO.Path]::GetDirectoryName([string]$request.image_path), 'Logs')
        if ($request.machine -notin @('M1','S3') -or [string]::IsNullOrWhiteSpace($path) -or
            -not [string]::Equals([IO.Path]::GetFullPath([IO.Path]::GetDirectoryName($path)), [IO.Path]::GetFullPath($directory), [StringComparison]::OrdinalIgnoreCase)) { throw 'Job proof is outside the selected controller log directory.' }
        $selected=SelectJobProofLog $directory ([string]$request.machine)
        if ($null -eq $selected -or -not [string]::Equals($selected.FullName, [IO.Path]::GetFullPath($path), [StringComparison]::OrdinalIgnoreCase)) { StopObservation 'The selected controller job log changed.' }
        $length=$selected.Length; $mtime=$selected.LastWriteTimeUtc.Ticks
        if ($length -lt $offset -or ($length - $offset) -gt 1048576) { StopObservation 'Controller job log was replaced or advanced beyond the verification bound.' }
        $buffer = New-Object byte[] ([int]($length - $start))
        $again = New-Object byte[] $buffer.Length
        foreach ($bytes in @($buffer,$again)) {
            # Reopen each pass: atomic replacement must not leave both checks
            # on a retired handle. Reselect before and after every bounded read.
            $current=SelectJobProofLog $directory ([string]$request.machine)
            if ($null -eq $current -or $current.FullName -cne $selected.FullName -or
                $current.Length -ne $length -or $current.LastWriteTimeUtc.Ticks -ne $mtime) { StopObservation 'Controller log selection changed during verification.' }
            $stream=[IO.File]::Open($path,[IO.FileMode]::Open,[IO.FileAccess]::Read,([IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete))
            try {
                if ($stream.Length -ne $length) { StopObservation 'Controller job log length changed.' }
                [void]$stream.Seek($start,[IO.SeekOrigin]::Begin)
                $read=0
                while ($read -lt $bytes.Length) {
                    $part=$stream.Read($bytes,$read,$bytes.Length-$read)
                    if ($part -eq 0) { StopObservation 'Incomplete controller job log read.' }; $read+=$part
                }
                if ($stream.Length -ne $length) { StopObservation 'Controller job log is being written.' }
            } finally { $stream.Dispose(); $stream=$null }
            $current=SelectJobProofLog $directory ([string]$request.machine)
            if ($null -eq $current -or $current.FullName -cne $selected.FullName -or
                $current.Length -ne $length -or $current.LastWriteTimeUtc.Ticks -ne $mtime) { StopObservation 'Controller log selection changed after verification.' }
        }
        $sha = [Security.Cryptography.SHA256]::Create()
        $prefix = ([BitConverter]::ToString($sha.ComputeHash($buffer,0,[int]($offset - $start)))).Replace('-','').ToLowerInvariant()
        if ($prefix -cne $expectedHash -or
            [Convert]::ToBase64String($buffer) -cne [Convert]::ToBase64String($again)) { StopObservation 'Controller job evidence changed after calculation.' }
        if ($buffer.Length -eq 0 -or $buffer[$buffer.Length-1] -ne 10) { StopObservation 'Incomplete controller job log tail.' }
        $tail = [Text.Encoding]::UTF8.GetString($buffer,[int]($offset-$start),[int]($length-$offset))
        foreach ($line in ($tail -split "`n")) {
            if ($line -notmatch 'AUTO') { continue }
            if ($line.Contains('AutoPrint started!') -or $line.Contains('AutoPrint finished!') -or
                $line.Contains('AutoPrint cancelled.') -or $line.Contains('Stopping printing as requested.')) { StopObservation 'Printing job changed after calculation; old decision discarded.' }
            if ($line -match '\bLayer:\s*(\d+)\s*->\s*Current Z\s*=') { $layer = [long]$matches[1] }
        }
        if ($layer -lt $index -or $layer -gt ($index+1)) { StopObservation 'The production layer advanced beyond the completed CSV decision.' }
    } catch {
        if ($script:observationWait) { throw }
        StopObservation ('Current controller job could not be verified: ' + $_.Exception.Message)
    } finally {
        if ($null -ne $sha) { $sha.Dispose() }
        if ($null -ne $stream) { $stream.Dispose() }
    }
}

function FiniteNumber([string]$text) {
    $number = 0.0
    if (-not [double]::TryParse($text.Replace(',','.'), [Globalization.NumberStyles]::Float,
        [Globalization.CultureInfo]::InvariantCulture, [ref]$number) -or [double]::IsNaN($number) -or [double]::IsInfinity($number)) {
        throw 'A finite numeric IR/temperature value is required.'
    }
    return $number
}

function StopObservation([string]$message) {
    # Only explicit read-only evidence changes get the recoverable type.
    # Unknown I/O/helper failures retain their original terminal classification.
    $script:observationWait = $true
    throw $message
}

function CsvContentHash([string]$path, [long]$expectedSize) {
    if ($expectedSize -le 0 -or $expectedSize -gt 33554432) { throw 'Monitoring CSV content verification exceeds its bounded size.' }
    $stream = $null; $sha = $null
    try {
        $stream = [IO.File]::Open($path, [IO.FileMode]::Open, [IO.FileAccess]::Read, ([IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete))
        if ($stream.Length -ne $expectedSize) { StopObservation 'Monitoring CSV size changed before content verification.' }
        $sha = [Security.Cryptography.SHA256]::Create()
        $buffer = New-Object byte[] 65536
        [long]$count = 0
        while ($count -le $expectedSize) {
            $part = $stream.Read($buffer, 0, [int][Math]::Min($buffer.Length, $expectedSize + 1 - $count))
            if ($part -eq 0) { break }
            $count += $part
            if ($count -gt $expectedSize) { StopObservation 'Monitoring CSV grew during content verification.' }
            [void]$sha.TransformBlock($buffer, 0, $part, $buffer, 0)
        }
        if ($count -ne $expectedSize -or $stream.Length -ne $expectedSize) { StopObservation 'Monitoring CSV changed during content verification.' }
        [void]$sha.TransformFinalBlock([byte[]]@(), 0, 0)
        return ([BitConverter]::ToString($sha.Hash)).Replace('-', '').ToLowerInvariant()
    } finally {
        if ($null -ne $sha) { $sha.Dispose() }
        if ($null -ne $stream) { $stream.Dispose() }
    }
}

function AssertCsvFile($token, $observation) {
    $script:observationWait = $false
    $path = [string](ValueOf $token 'path')
    if ([string]::IsNullOrWhiteSpace($path) -or [IO.Path]::GetFileName($path) -notmatch '^LayerDatasheet_\d{8}_\d{6}\.csv$') { throw 'A verified Monitoring CSV token is required.' }
    $expectedHash = [string](ValueOf $token 'sha256')
    if ($expectedHash -cnotmatch '^[0-9a-f]{64}$') { throw 'A SHA256-bound Monitoring CSV token is required.' }
    $sizeValue = ValueOf $token 'size'
    if ($sizeValue -isnot [int] -and $sizeValue -isnot [long]) { throw 'An integer Monitoring CSV size is required.' }
    $expectedSize = [long]$sizeValue
    if ($expectedSize -le 0 -or $expectedSize -gt 33554432) { throw 'Monitoring CSV exceeds the content verification bound.' }
    $mtimeValue = ValueOf $token 'mtime_ticks'
    if ($mtimeValue -isnot [int] -and $mtimeValue -isnot [long]) { throw 'Integer Monitoring CSV UTC ticks are required.' }
    $expectedMtime = [long]$mtimeValue
    if ($expectedMtime -le 0 -or $expectedMtime -gt [DateTime]::MaxValue.Ticks) { throw 'Monitoring CSV UTC ticks are outside the supported range.' }
    $file = New-Object IO.FileInfo($path)
    $file.Refresh()
    if (-not $file.Exists -or $file.Length -ne $expectedSize -or
        $file.LastWriteTimeUtc.Ticks -ne $expectedMtime) {
        StopObservation 'The Monitoring CSV changed after the decision; no settings were saved.'
    }
    # Reopen for the second pass so an atomic replacement cannot leave both
    # checks reading an old, still-open handle. This is bounded evidence,
    # not an atomic lock with the later device operation.
    for ($pass = 0; $pass -lt 2; $pass++) {
        if ((CsvContentHash $path $expectedSize) -cne $expectedHash) { StopObservation 'The Monitoring CSV content changed after the decision.' }
        $file.Refresh()
        if (-not $file.Exists -or $file.Length -ne $expectedSize -or
            $file.LastWriteTimeUtc.Ticks -ne $expectedMtime) { StopObservation 'The Monitoring CSV changed during verification.' }
    }
    $age = ([DateTime]::UtcNow - $file.LastWriteTimeUtc).TotalSeconds
    if ($age -lt -2 -or $age -gt 5) { throw 'The completed CSV record is no longer fresh enough for an IR update.' }
    $index = [int]$observation.row_index
    AssertJobProof (ValueOf $token 'job') $index
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

function ImportBundledBridge([string]$path) {
    # Pin the reviewed bridge bytes, including for Internet-zone downloads.
    # Rebuilding UiaBridge.dll requires review and updating this digest.
    $expected = 'aa9083d1c43256fd3ad34fe84d7e266b0958c6ad6164b1159ba6a18c2db06f1e'
    $stream = $null; $sha = $null
    try {
        if (-not [IO.File]::Exists($path)) { throw 'Bundled UI bridge file is missing.' }
        $stream = [IO.File]::Open($path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
        $size = $stream.Length
        if ($size -le 0 -or $size -gt 1048576) { throw 'Bundled UI bridge exceeds its verified size bound.' }
        $bytes = New-Object byte[] ([int]$size)
        $read = 0
        while ($read -lt $bytes.Length) {
            $part = $stream.Read($bytes, $read, $bytes.Length - $read)
            if ($part -eq 0) { throw 'Bundled UI bridge is truncated.' }
            $read += $part
        }
        if ($stream.Length -ne $size) { throw 'Bundled UI bridge changed during verification.' }
        $sha = [Security.Cryptography.SHA256]::Create()
        $digest = ([BitConverter]::ToString($sha.ComputeHash($bytes))).Replace('-', '').ToLowerInvariant()
        if ($digest -cne $expected) { throw 'Bundled UI bridge SHA-256 verification failed. Extract the complete trusted release again.' }
    } finally {
        if ($null -ne $sha) { $sha.Dispose() }
        if ($null -ne $stream) { $stream.Dispose() }
    }
    # Dependencies are Windows framework assemblies, never arbitrary package paths.
    Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes, Accessibility
    [void][Reflection.Assembly]::Load([byte[]]$bytes)
}


function SequenceMetadata($root, $pidExpected) {
    $script:controlWait = $true
    $saveVisible = $false
    foreach ($control in @([BinderJetNativeProcess]::Enumerate($root, $pidExpected))) {
        if ($control.visible -and [string]$control.class_name -match '(?i)BUTTON' -and
            ([string]$control.text).Trim() -ieq 'Save') { $saveVisible = $true }
    }
    if (-not $saveVisible) { throw 'The Sequence Save button is not visible; leaf metadata was not collected.' }
    return @([BinderJetLeafMetadata]::Collect($root, $pidExpected))
}

function ScopeProof($root, $pidExpected, $handles) {
    $scope = HandleOf $handles 'scope'
    $scopeId = [BinderJetLeafMetadata]::ScopeIdentity($root, $scope, $pidExpected)
    $proof = @{scope_hwnd=$scope.ToInt64(); scope_class=[string]$scopeId.class_name; scope_parent=[long]$scopeId.parent; scope_thread=[long]$scopeId.thread; scope_control_id=[int]$scopeId.control_id}
    foreach ($name in @('ir','save','machine','state')) {
        $window = HandleOf $handles $name
        $leaf = [BinderJetLeafMetadata]::LeafIdentity($root, $window, $pidExpected)
        $expectedId = (@{ir='textBoxHeatingOutput';save='btnSave';machine='labelMachineName';state='labelTaskStatus'})[$name]
        if ([string]$leaf.id -cne $expectedId) { throw 'An audited anchor AutomationId changed during preparation.' }
        if ($name -in @('machine','state') -and [string]$leaf.class_name -notmatch '(?i)STATIC') { throw 'An audited global label class changed.' }
        $proof[$name + '_hwnd'] = $window.ToInt64()
        $proof[$name + '_class'] = [string]$leaf.class_name
        $proof[$name + '_id'] = [string]$leaf.id
        if ($name -in @('machine','state')) {
            $proof[$name + '_parent'] = [long]$leaf.parent
            $proof[$name + '_thread'] = [long]$leaf.thread
            $proof[$name + '_control_id'] = [int]$leaf.control_id
        }
    }
    return $proof
}

function AssertProofShape($handles) {
    $proof = ValueOf $handles 'scope_proof'
    if ($null -eq $proof) { throw 'Audited identity proof is unavailable.' }
    foreach ($key in @('scope_hwnd','scope_parent','scope_thread','scope_control_id','scope_class',
        'ir_hwnd','ir_class','ir_id','save_hwnd','save_class','save_id',
        'machine_hwnd','machine_class','machine_id','machine_parent','machine_thread','machine_control_id',
        'state_hwnd','state_class','state_id','state_parent','state_thread','state_control_id')) {
        if ($null -eq (ValueOf $proof $key)) { throw 'Audited identity proof is incomplete.' }
    }
    foreach ($key in @('scope_hwnd','scope_parent','scope_thread','ir_hwnd','save_hwnd',
        'machine_hwnd','machine_parent','machine_thread','state_hwnd','state_parent','state_thread')) {
        if ([long](ValueOf $proof $key) -le 0) { throw 'Audited identity proof is invalid.' }
    }
    foreach ($key in @('scope_class','ir_class','save_class','machine_class','state_class')) {
        if ([string]::IsNullOrWhiteSpace([string](ValueOf $proof $key))) { throw 'Audited identity class is missing.' }
    }
    foreach ($pair in @(@('ir','textBoxHeatingOutput'),@('save','btnSave'),@('machine','labelMachineName'),@('state','labelTaskStatus'))) {
        if ([string](ValueOf $proof ($pair[0] + '_id')) -cne $pair[1]) { throw 'Audited identity AutomationId is invalid.' }
    }
}

function AssertGlobalProof($root, $pidExpected, $handles) {
    AssertProofShape $handles
    $proof = ValueOf $handles 'scope_proof'
    foreach ($name in @('machine','state')) {
        $window = HandleOf $handles $name
        if ($window -eq [IntPtr]::Zero -or $window.ToInt64() -ne [long](ValueOf $proof ($name + '_hwnd'))) { throw 'An audited global anchor changed.' }
        $leaf = [BinderJetLeafMetadata]::LeafIdentity($root, $window, $pidExpected)
        if ([string]$leaf.class_name -cne [string](ValueOf $proof ($name + '_class')) -or
            [string]$leaf.id -cne [string](ValueOf $proof ($name + '_id')) -or
            [long]$leaf.parent -ne [long](ValueOf $proof ($name + '_parent')) -or
            [long]$leaf.thread -ne [long](ValueOf $proof ($name + '_thread')) -or
            [int]$leaf.control_id -ne [int](ValueOf $proof ($name + '_control_id'))) { throw 'An audited global anchor identity changed.' }
    }
}

function ScopeStructureMatches($root, $pidExpected, $handles) {
    AssertProofShape $handles
    $proof = ValueOf $handles 'scope_proof'
    if ($null -eq $proof) { throw 'Audited Sequence scope proof is unavailable.' }
    $scope = HandleOf $handles 'scope'; $ir = HandleOf $handles 'ir'; $save = HandleOf $handles 'save'
    if ($scope -eq $root -or $ir -eq $root -or $save -eq $root -or
        $scope -eq $ir -or $scope -eq $save -or $ir -eq $save) {
        throw 'The audited Sequence scope and leaf handles are malformed.'
    }
    foreach ($name in @('scope','ir','save')) {
        $window = HandleOf $handles $name
        if ($window -eq [IntPtr]::Zero -or $window.ToInt64() -ne [long](ValueOf $proof ($name + '_hwnd'))) { throw 'Audited Sequence proof and handles disagree.' }
    }
    return [BinderJetLeafMetadata]::ScopeStructureMatches($root, (HandleOf $handles 'scope'),
        (HandleOf $handles 'ir'), (HandleOf $handles 'save'), $pidExpected,
        [string](ValueOf $proof 'scope_class'), [long](ValueOf $proof 'scope_parent'),
        [long](ValueOf $proof 'scope_thread'), [int](ValueOf $proof 'scope_control_id'),
        [string](ValueOf $proof 'ir_class'), [string](ValueOf $proof 'save_class'))
}

function VisibleReaudit($root, $pidExpected, $handles) {
    # Only called for missing proof or positively identified structural staleness.
    # This old collector admits visible controls only and never opens a view.
    $hasProof = $null -ne (ValueOf $handles 'scope_proof')
    if ($hasProof) {
        AssertGlobalProof $root $pidExpected $handles
        AssertAutomatic $root $pidExpected $handles
    }
    $script:controlWait = $true
    $records = @(SequenceMetadata $root $pidExpected)
    $fresh = PreparedControl $root $pidExpected $records
    if ($hasProof) {
        foreach ($name in @('machine','state')) {
            if ((HandleOf $fresh.handles $name) -ne (HandleOf $handles $name)) { throw 'Visible re-audit changed an audited global anchor.' }
        }
        AssertGlobalProof $root $pidExpected $handles
    }
    AssertAutomatic $root $pidExpected $fresh.handles
    $script:controlWait = $true
    $fresh.state = ([BinderJetNativeProcess]::ReadText($root, (HandleOf $fresh.handles 'state'), $pidExpected)).Trim()
    if ($fresh.state -notin @('AUTO','RUNNING')) { throw 'Automatic recipe updates require a fresh AUTO/RUNNING state.' }
    return $fresh
}

function AssertScopeProof($root, $pidExpected, $handles) {
    AssertGlobalProof $root $pidExpected $handles
    $proof = ValueOf $handles 'scope_proof'
    if ($null -eq $proof) { throw 'A previously audited Sequence scope proof is required.' }
    $scope = HandleOf $handles 'scope'
    if ($scope -eq [IntPtr]::Zero -or $scope -eq $root -or $scope.ToInt64() -ne [long](ValueOf $proof 'scope_hwnd')) { throw 'The audited Sequence scope changed.' }
    $scopeId = [BinderJetLeafMetadata]::ScopeIdentity($root, $scope, $pidExpected)
    if ([string]$scopeId.class_name -cne [string](ValueOf $proof 'scope_class') -or
        [long]$scopeId.parent -ne [long](ValueOf $proof 'scope_parent') -or
        [long]$scopeId.thread -ne [long](ValueOf $proof 'scope_thread') -or
        [int]$scopeId.control_id -ne [int](ValueOf $proof 'scope_control_id')) { throw 'The audited Sequence scope identity changed.' }
    foreach ($name in @('ir','save')) {
        $window = HandleOf $handles $name
        if ($window -eq [IntPtr]::Zero -or $window.ToInt64() -ne [long](ValueOf $proof ($name + '_hwnd'))) { throw 'An audited native anchor changed.' }
        $leaf = [BinderJetLeafMetadata]::LeafIdentity($root, $window, $pidExpected)
        if ([string]$leaf.class_name -cne [string](ValueOf $proof ($name + '_class')) -or
            [string]$leaf.id -cne [string](ValueOf $proof ($name + '_id'))) { throw 'An audited native anchor identity changed.' }
    }
    $ir = HandleOf $handles 'ir'; $save = HandleOf $handles 'save'
    if ([BinderJetLeafMetadata]::CommonAncestor($root, $ir, $save, $pidExpected) -ne $scope) { throw 'IR and Save left the audited Sequence scope.' }
    [BinderJetNativeFields]::ValidateWritableIr($root, $ir, $pidExpected)
    [BinderJetNativeFields]::ValidateSave($root, $save, $pidExpected)
}

function ScopedInventorySignature($records) {
    return (@($records | Sort-Object { [long]$_.hwnd } | ForEach-Object {
        @([long]$_.hwnd, [string]$_.class_name, [string]$_.id, [bool]$_.enabled)
    }) | ConvertTo-Json -Depth 6 -Compress)
}

function ScopedPreparedControl($root, $pidExpected, $handles) {
    $script:controlWait = $true
    AssertScopeProof $root $pidExpected $handles
    $machine = NativeMachine $root $pidExpected $handles
    if ($machine -notin @('M1','S3') -or $machine -cne [string]$request.machine) { $script:identityFailure = $true; throw 'The selected machine identity changed.' }
    AssertAutomatic $root $pidExpected $handles
    $script:controlWait = $true
    $scope = HandleOf $handles 'scope'
    $records = @([BinderJetLeafMetadata]::CollectScoped($root, $scope, $pidExpected))
    if ($records.Count -eq 0 -or $records.Count -gt 900) { throw 'A complete bounded Sequence inventory is required.' }
    $ir = UniqueRecord $records 'textBoxHeatingOutput'
    $save = UniqueRecord $records 'btnSave'
    if ($null -eq $ir -or $null -eq $save -or [long]$ir.hwnd -ne (HandleOf $handles 'ir').ToInt64() -or
        [long]$save.hwnd -ne (HandleOf $handles 'save').ToInt64()) { throw 'The audited IR or Save is hidden or changed.' }
    $map = @{}
    foreach ($record in $records) {
        $window = [IntPtr][long]$record.hwnd
        $kind = [BinderJetNativeFields]::Kind($root, $window, $pidExpected)
        if ($kind -notin @('text','toggle')) { continue }
        $id = [string]$record.id
        if ([string]::IsNullOrWhiteSpace($id) -or $map.ContainsKey($id)) { throw 'A Sequence input has a blank or duplicate AutomationId.' }
        $map[$id] = @{hwnd=$window.ToInt64();kind=$kind}
    }
    if ($map.Count -gt 256 -or -not $map.ContainsKey('textBoxHeatingOutput')) { throw 'The bounded Sequence input map is incomplete.' }
    $fields = ReadFields $root $pidExpected $map $scope
    $after = @([BinderJetLeafMetadata]::CollectScoped($root, $scope, $pidExpected))
    if ((ScopedInventorySignature $records) -cne (ScopedInventorySignature $after)) { throw 'The Sequence inventory changed during field reads.' }
    AssertScopeProof $root $pidExpected $handles
    $afterMachine = NativeMachine $root $pidExpected $handles
    if ($afterMachine -cne $machine) { $script:identityFailure = $true; throw 'The selected machine identity changed during recovery.' }
    AssertAutomatic $root $pidExpected $handles
    $script:controlWait = $true
    $state = ([BinderJetNativeProcess]::ReadText($root, (HandleOf $handles 'state'), $pidExpected)).Trim()
    if ($state -notin @('AUTO','RUNNING')) { throw 'Automatic recipe updates require a fresh AUTO/RUNNING state.' }
    return @{success=$true;machine=$machine;fields=$fields;field_map=$map;handles=$handles;setting=$fields['textBoxHeatingOutput'];state=$state}
}

function PreparedControl($root, $pidExpected, $records) {
    $script:controlWait = $true
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
    if ($verifiedMachine -notin @('M1','S3') -or ($request.machine -and $verifiedMachine -cne [string]$request.machine)) { if ($verifiedMachine -in @('M1','S3')) { $script:identityFailure = $true }; throw 'Control requires a verified exact M1/S3 native machine label.' }
    $newHandles['scope_proof'] = ScopeProof $root $pidExpected $newHandles
    return @{success=$true;machine=$verifiedMachine;fields=$fields;field_map=$map;handles=$newHandles;setting=$fields['textBoxHeatingOutput']}
}

$script:identityFailure = $false
$script:controlWait = $false

try {
    $request = [Console]::In.ReadToEnd() | ConvertFrom-Json
    ImportBundledBridge ([string]$request.bridge_path)
    if ($request.operation -eq 'discover') {
        $windows = New-Object 'Collections.Generic.List[object]'
        $complete=$true; $visited=0
        $instance = $null
        try {
            $registry = [IO.Path]::Combine([Environment]::GetFolderPath('LocalApplicationData'), 'BinderJet.Unified', 'runtime-instance.json')
            $info = New-Object IO.FileInfo($registry)
            if ($info.Exists -and $info.Length -le 65536) { $instance = [IO.File]::ReadAllText($registry) | ConvertFrom-Json }
        } catch { }
        foreach ($process in Get-Process) {
            if (++$visited -gt 4096) { $complete=$false;break }
            $title = [string]$process.MainWindowTitle; $name = [string]$process.ProcessName
            if (($title + ' ' + $name) -notmatch '(?i)Binder\s*Jet|BinderJet|BinderjetController') { continue }
            $window = $process.MainWindowHandle
            if ($window -eq [IntPtr]::Zero) { $window = [BinderJetNativeProcess]::FindRoot([int]$process.Id) }
            if ($window -eq [IntPtr]::Zero) { continue }
            $image = ''; try { $image = [string]$process.Path } catch { }
            if ($request.expected_image_path -and [string]::IsNullOrWhiteSpace($image)) { $complete=$false;continue }
            if ($request.expected_image_path -and -not [string]::Equals($image,[string]$request.expected_image_path,[StringComparison]::OrdinalIgnoreCase)) { continue }
            $machine = 'unknown'
            if ($null -ne $instance -and [int]$instance.processId -eq $process.Id -and [string]$instance.machineName -in @('M1','S3')) { $machine = [string]$instance.machineName }
            if ($machine -eq 'unknown') { $machine = MachineKind ($title + ' ' + [IO.Path]::GetFileName($image)) }
            if ($machine -eq 'unknown' -and [BinderJetQueuedNavigation]::Responsive($window,[int]$process.Id,100)) {
                try { $machine = [BinderJetNativeProcess]::IdentifyMachine($window, [int]$process.Id) } catch { }
            }
            if ($request.expected_machine) {
                # Captions/filenames/registry hints cannot prove uniqueness of
                # the machine partition during automatic reconnection.
                $machine='unknown'
                if ([BinderJetQueuedNavigation]::Responsive($window,[int]$process.Id,100)) {
                    try { $machine=[BinderJetNativeProcess]::IdentifyMachine($window,[int]$process.Id) } catch { }
                }
            }
            if ($request.expected_machine -and $machine -eq 'unknown') { $complete=$false }
            if ($request.expected_machine -and $machine -ne [string]$request.expected_machine) { continue }
            if ($windows.Count -ge 12) { $complete=$false;break }
            $windows.Add(@{machine=$machine;pid=$process.Id;hwnd=$window.ToInt64();title=$title;image_path=$image})
        }
        $response = @{success=$true;windows=@($windows.ToArray());complete=$complete}
    } else {
        $root = [IntPtr][long]$request.hwnd; $pidExpected = [int]$request.pid; $handles = $request.handles
        # Identity validation does not enter the target window procedure.
        $script:identityFailure = $true
        [BinderJetNativeProcess]::ValidateWindow($root, $pidExpected)
        $script:identityFailure = $false
        $machine = if ($request.operation -eq 'validate') { [string]$request.machine } elseif ($request.operation -eq 'recover_control' -and $null -ne (ValueOf $handles 'scope_proof')) {
            # Validate exact cached global handles first; NativeMachine otherwise
            # allows visible-root discovery when a machine handle is missing.
            $script:controlWait = $true
            AssertGlobalProof $root $pidExpected $handles
            CheckMachine $root $pidExpected $handles
        } elseif ($request.operation -eq 'recover_control') {
            try { CheckMachine $root $pidExpected $handles } catch {
                if ($script:identityFailure) { throw }
                CheckMachine $root $pidExpected @{}
            }
        } else { CheckMachine $root $pidExpected $handles }
        switch ([string]$request.operation) {
            'validate' {
                $response = @{success=$true;machine=$machine;handles=@{}}
            }
            'read_status' {
                $script:controlWait = $true
                $stateHandle = HandleOf $handles 'state'
                if ($stateHandle -eq [IntPtr]::Zero -and $request.bootstrap_status -eq $true -and
                    $null -eq (ValueOf $handles 'scope_proof')) {
                    # Rebound endpoints have no cached controls yet. Discover
                    # exact global labels read-only; do not enter any page.
                    $records=@(foreach ($candidate in @([BinderJetNativeProcess]::Enumerate($root, $pidExpected))) {
                        if ([string]$candidate.class_name -notmatch '(?i)STATIC') { continue }
                        $leaf=[BinderJetLeafMetadata]::LeafIdentity($root,[IntPtr][long]$candidate.hwnd,$pidExpected)
                        if ([string]$leaf.id -in @('labelTaskStatus','labelMachineName')) {
                            @{hwnd=$candidate.hwnd;id=[string]$leaf.id;class_name=$candidate.class_name;visible=$true}
                        }
                    })
                    $record=UniqueVisibleRecord $records 'labelTaskStatus' $true
                    if ([string]$record.class_name -notmatch '(?i)STATIC') { throw 'The task-state control is not a native label.' }
                    $stateHandle=[IntPtr][long]$record.hwnd
                    $machineRecord=UniqueVisibleRecord $records 'labelMachineName' $true
                    $handles=@{state=$stateHandle.ToInt64();machine=[long]$machineRecord.hwnd}
                    $machine=CheckMachine $root $pidExpected $handles
                }
                $script:controlWait = $true
                if (-not (Owned $root $stateHandle $pidExpected)) { throw 'The audited task-state label is unavailable.' }
                $state = ([BinderJetNativeProcess]::ReadText($root, $stateHandle, $pidExpected)).Trim()
                $response = @{success=$true;machine=$machine;state=$state;handles=$handles}
            }
            'enter_sequence' {
                # A native menu click creates/shows the existing Sequence view.
                # Never identify, read, scroll or open the Process table.
                ClickNavigation $root $pidExpected $handles 'btnSequences'
                $response = @{success=$true;machine=$machine;entered=$true}
            }
            'metadata' {
                $response = @{success=$true;records=@(SequenceMetadata $root $pidExpected)}
            }
            'prepare_control' {
                $records = @($request.metadata | Where-Object { $null -ne $_ })
                if ($records.Count -eq 0) { $records = @($request.records | Where-Object { $null -ne $_ }) }
                $response = PreparedControl $root $pidExpected $records
            }
            'recover_control' {
                # A single bounded READ-ONLY operation; no navigation, IR edit or Save.
                $script:controlWait = $true
                if ([BinderJetQueuedNavigation]::IsUserEditing($root, $pidExpected)) { throw 'The user is editing a target field; automatic Save is suspended.' }
                if ($null -ne (ValueOf $handles 'scope_proof')) {
                    # No general catch: provider, global identity and scoped inventory
                    # failures cannot fall back to a broader inventory.
                    AssertGlobalProof $root $pidExpected $handles
                    if (ScopeStructureMatches $root $pidExpected $handles) {
                        $response = ScopedPreparedControl $root $pidExpected $handles
                    } else {
                        $response = VisibleReaudit $root $pidExpected $handles
                    }
                } else {
                    $response = VisibleReaudit $root $pidExpected $handles
                }
            }
            'verify_control' {
                AssertAutomatic $root $pidExpected $handles
                AssertCsvFile $request.csv_token $request.observation
                $script:controlWait = $true
                $scope = HandleOf $handles 'scope'
                if (-not (Owned $root $scope $pidExpected)) { throw 'The audited Sequence view identity changed.' }
                $fields = ReadFields $root $pidExpected $request.field_map $scope
                $response = @{success=$true;fields=$fields;setting=$fields['textBoxHeatingOutput'];handles=$handles;field_map=$request.field_map}
            }
            'apply' {
                AssertAutomatic $root $pidExpected $handles
                $script:controlWait = $true
                $scope = HandleOf $handles 'scope'; $ir = HandleOf $handles 'ir'; $save = HandleOf $handles 'save'
                if (-not (Owned $root $scope $pidExpected)) { throw 'The controlled Sequence view identity changed.' }
                if (-not [BinderJetLeafMetadata]::IsDescendant($root, $scope, $ir, $pidExpected) -or
                    -not [BinderJetLeafMetadata]::IsDescendant($root, $scope, $save, $pidExpected)) { throw 'IR/Save no longer belong to the controlled Sequence view.' }
                $irMap = ValueOf $request.field_map 'textBoxHeatingOutput'
                if ($null -eq $irMap -or [long](ValueOf $irMap 'hwnd') -ne $ir.ToInt64() -or [string](ValueOf $irMap 'kind') -cne 'text') { throw 'The IR field map no longer matches its verified native handle.' }
                $before = ReadFields $root $pidExpected $request.field_map $scope
                AssertOtherFields $before $request.baseline
                if (-not $before.ContainsKey('textBoxHeatingOutput') -or [string]$before['textBoxHeatingOutput'] -cne [string]$request.expected_ir) { throw 'The IR setting changed outside this module; no settings were saved.' }
                $script:controlWait = $false
                $desired = FiniteNumber ([string]$request.power)
                if ($desired -lt 0 -or $desired -gt 100) { throw 'IR recipe power must be in 0..100.' }
                if ([string]::IsNullOrWhiteSpace([string]$request.edit_marker) -or [string]::IsNullOrWhiteSpace([string]$request.save_marker)) { throw 'Write-ahead edit/Save markers are required.' }
                $script:controlWait = $true
                [BinderJetNativeFields]::ValidateSave($root, $save, $pidExpected)
                AssertAutomatic $root $pidExpected $handles
                $script:controlWait = $false
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
    $kind = if ($script:identityFailure) { 'identity' } elseif ($script:observationWait) { 'observation_wait' } elseif ($script:controlWait) { 'control_wait' } else { 'operation' }
    @{success=$false;error=$_.Exception.Message;error_kind=$kind} | ConvertTo-Json -Depth 3 -Compress
    exit 1
}
