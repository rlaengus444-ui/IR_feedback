param([string]$PackageRoot, [string]$ResultPath)
$ErrorActionPreference = 'Stop'
$helper = Join-Path $PackageRoot 'app/native_helper.ps1'
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($helper, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw "Native helper parse errors: $errors" }
# Load only the actual read-only guard functions, never the UI dispatcher.
foreach ($name in @('ValueOf', 'StopObservation', 'CsvContentHash', 'SelectJobProofLog', 'AssertJobProof', 'AssertCsvFile')) {
    $definitions = @($ast.FindAll({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name }, $true))
    if ($definitions.Count -ne 1) { throw "Cannot identify exact production function $name" }
    Invoke-Expression $definitions[0].Extent.Text
}
$directory = Join-Path $env:TEMP ([Guid]::NewGuid().ToString('N'))
[void][IO.Directory]::CreateDirectory($directory)
$request = @{ machine = 'M1'; image_path = (Join-Path $directory 'Controller.exe') }
[void][IO.Directory]::CreateDirectory((Join-Path $directory 'Logs'))
$jobPath = Join-Path $directory 'Logs/20261111_M1.log'
$jobBytes = [Text.Encoding]::UTF8.GetBytes("2026-11-11AM090000...[BJT - AUTO]: AutoPrint started!`n2026-11-11AM090001...[BJT - AUTO]: Layer: 1 -> Current Z = 1`n")
[IO.File]::WriteAllBytes($jobPath,$jobBytes)
$jobSha = [Security.Cryptography.SHA256]::Create()
try { $jobHash = ([BitConverter]::ToString($jobSha.ComputeHash($jobBytes))).Replace('-','').ToLowerInvariant() } finally { $jobSha.Dispose() }
$jobProof = @{path=$jobPath;start=0;offset=$jobBytes.Length;sha256=$jobHash;run_key=('1'*64);layer=1}
$path = Join-Path $directory 'LayerDatasheet_20261028_000000.csv'
$checks = [Collections.Generic.List[string]]::new()
function TokenFor([byte[]]$bytes) {
    $sha = [Security.Cryptography.SHA256]::Create()
    try { $digest = ([BitConverter]::ToString($sha.ComputeHash($bytes))).Replace('-', '').ToLowerInvariant() }
    finally { $sha.Dispose() }
    return @{ path = $path; size = $bytes.Length; mtime_ticks = ([IO.FileInfo]::new($path)).LastWriteTimeUtc.Ticks; sha256 = $digest; job=$jobProof }
}
function MustReject($token, [string]$label, [bool]$observationWait = $false) {
    $rejected = $false
    try { AssertCsvFile $token @{ row_index = 0 } } catch { $rejected = $true }
    if (-not $rejected) { throw "Production guard accepted $label" }
    if ([bool]$script:observationWait -ne $observationWait) { throw "Wrong observation-wait classification for $label" }
    $checks.Add($label)
}
try {
    $raw = [Text.Encoding]::UTF8.GetBytes("Id,TemperaturePostRecoating`r`n1,42`r`n")
    [IO.File]::WriteAllBytes($path, $raw)
    $token = TokenFor $raw
    AssertCsvFile $token @{ row_index = 0 }
    $checks.Add('unchanged content accepted')
    $noJob=$token.Clone();$noJob.Remove('job')
    MustReject $noJob 'missing native job proof holds' $true
    foreach ($tail in @(
        "2026-11-11AM100000...[BJT - AUTO]: AutoPrint started!`n2026-11-11AM100001...[BJT - AUTO]: Layer: 1 -> Current Z = 1`n",
        "2026-11-11AM100000...[BJT - AUTO]: AutoPrint cancelled.`n",
        "2026-11-11AM100000...[BJT - AUTO]: Layer: 3 -> Current Z = 1`n",
        'partial tail')) {
        [IO.File]::WriteAllBytes($jobPath,$jobBytes)
        [IO.File]::AppendAllText($jobPath,$tail)
        MustReject $token ('native job switch/stop/advance/incomplete tail: '+$tail.Trim()) $true
    }
    [IO.File]::WriteAllBytes($jobPath,$jobBytes)
    [IO.File]::AppendAllText($jobPath,"2026-11-11AM100000...[BJT - AUTO]: Layer: 1 -> Current Z = 1`n")
    AssertCsvFile $token @{row_index=0}
    $checks.Add('ordinary same-run layer append remains usable')
    [IO.File]::WriteAllBytes($jobPath,$jobBytes)
    $changedJob=[Text.Encoding]::UTF8.GetBytes(([Text.Encoding]::UTF8.GetString($jobBytes)).Replace('090000','080000'))
    [IO.File]::WriteAllBytes($jobPath,$changedJob)
    MustReject $token 'rewritten job anchor rejected with unchanged CSV' $true
    [IO.File]::Delete($jobPath)
    MustReject $token 'missing controller log holds' $true
    [IO.File]::WriteAllBytes($jobPath,$jobBytes)
    # Interleave replacement/new-log selection with the actual production
    # selection function; guard read/hash code remains byte-for-byte production.
    $originalSelect=${function:SelectJobProofLog}
    foreach ($raceCase in @('replace','new-selection')) {
        [IO.File]::WriteAllBytes($jobPath,$jobBytes)
        $raceMtime=[IO.File]::GetLastWriteTimeUtc($jobPath)
        $script:selectionCalls=0
        function SelectJobProofLog([string]$directory,[string]$machine) {
            $selected=& $originalSelect $directory $machine
            $script:selectionCalls++
            if ($script:selectionCalls -eq 3) {
                if ($raceCase -eq 'replace') {
                    $tmp=$jobPath+'.tmp'
                    [IO.File]::WriteAllBytes($tmp,$changedJob)
                    [IO.File]::SetLastWriteTimeUtc($tmp,$raceMtime)
                    [IO.File]::Replace($tmp,$jobPath,$null)
                } else {
                    $newLog=Join-Path ([IO.Path]::GetDirectoryName($jobPath)) '20261112_M1.log'
                    [IO.File]::WriteAllBytes($newLog,$changedJob)
                    [IO.File]::SetLastWriteTimeUtc($newLog,$raceMtime.AddSeconds(1))
                }
            }
            return $selected
        }
        MustReject $token ('native read race '+$raceCase) $true
        Remove-Item (Join-Path ([IO.Path]::GetDirectoryName($jobPath)) '20261112_M1.log') -ErrorAction SilentlyContinue
        Set-Item Function:SelectJobProofLog $originalSelect
    }
    [IO.File]::WriteAllBytes($jobPath,$jobBytes)
    $modified = [Text.Encoding]::UTF8.GetBytes("Id,TemperaturePostRecoating`r`n1,-1`r`n")
    [IO.File]::WriteAllBytes($path, $modified)
    [IO.File]::SetLastWriteTimeUtc($path, [DateTime]::new([long]$token.mtime_ticks, [DateTimeKind]::Utc))
    if ($modified.Length -ne $raw.Length -or ([IO.FileInfo]::new($path)).LastWriteTimeUtc.Ticks -ne $token.mtime_ticks) { throw 'Regression fixture did not preserve size and mtime' }
    MustReject $token 'same-size preserved-mtime 42 to -1 rejected' $true
    [IO.File]::WriteAllBytes($path, $raw)
    $token = TokenFor $raw
    [IO.File]::AppendAllText($path, '2,')
    MustReject $token 'partial append holds without edit or Save' $true
    [IO.File]::AppendAllText($path, "43`r`n")
    MustReject $token 'complete append holds without edit or Save' $true
    [IO.File]::WriteAllBytes($path, $raw)
    $token = TokenFor $raw
    $noHash = $token.Clone(); $noHash.Remove('sha256')
    MustReject $noHash 'missing hash rejected'
    $noMtime = $token.Clone(); $noMtime.Remove('mtime_ticks')
    MustReject $noMtime 'missing mtime rejected as terminal'
    foreach ($badMtime in @($null, $true, 'not ticks', [double]1.5, [long]0, [long]-1, ([DateTime]::MaxValue.Ticks + [long]1))) {
        $invalid = $token.Clone(); $invalid.mtime_ticks = $badMtime
        MustReject $invalid ('invalid mtime type/range rejected as terminal: ' + [string]$badMtime)
    }
    foreach ($badSize in @($null, $true, '32', [double]1.5)) {
        $invalid = $token.Clone(); $invalid.size = $badSize
        MustReject $invalid ('invalid size type rejected as terminal: ' + [string]$badSize)
    }
    $oversized = $token.Clone(); $oversized.size = 33554433
    MustReject $oversized 'oversized evidence rejected'
    $corrupt = $token.Clone(); $corrupt.sha256 = '0' * 64
    MustReject $corrupt 'mismatched digest rejected' $true
    $big = New-Object byte[] 33554432
    [IO.File]::WriteAllBytes($path, $big)
    $token = TokenFor $big
    $watch = [Diagnostics.Stopwatch]::StartNew()
    AssertCsvFile $token @{ row_index = 0 }
    $watch.Stop()
    $checks.Add('32 MiB bounded two-pass content assertion accepted')
    @{ passed = $true; checks = @($checks); maximum_csv_bytes = 33554432; two_pass_32mib_ms = $watch.Elapsed.TotalMilliseconds; hardware_actions = 0 } | ConvertTo-Json -Depth 4 | Set-Content -Encoding utf8 $ResultPath
} finally { [IO.Directory]::Delete($directory, $true) }
