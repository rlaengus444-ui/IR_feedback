param([string]$PackageRoot, [string]$ResultPath)
$ErrorActionPreference = 'Stop'
$helper = Join-Path $PackageRoot 'app/native_helper.ps1'
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($helper, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw "Native helper parse errors: $errors" }
# Load only the actual read-only guard functions, never the UI dispatcher.
foreach ($name in @('ValueOf', 'FreshLogHint', 'StopObservation', 'CsvContentHash', 'AssertCsvFile')) {
    $definitions = @($ast.FindAll({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name }, $true))
    if ($definitions.Count -ne 1) { throw "Cannot identify exact production function $name" }
    Invoke-Expression $definitions[0].Extent.Text
}
$request = @{ machine = 'unknown'; image_path = '' }
$directory = Join-Path $env:TEMP ([Guid]::NewGuid().ToString('N'))
[void][IO.Directory]::CreateDirectory($directory)
$path = Join-Path $directory 'LayerDatasheet_20261028_000000.csv'
$checks = [Collections.Generic.List[string]]::new()
function TokenFor([byte[]]$bytes) {
    $sha = [Security.Cryptography.SHA256]::Create()
    try { $digest = ([BitConverter]::ToString($sha.ComputeHash($bytes))).Replace('-', '').ToLowerInvariant() }
    finally { $sha.Dispose() }
    return @{ path = $path; size = $bytes.Length; mtime_ticks = ([IO.FileInfo]::new($path)).LastWriteTimeUtc.Ticks; sha256 = $digest }
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
