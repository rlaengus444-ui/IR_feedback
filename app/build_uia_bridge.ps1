$ErrorActionPreference = 'Stop'
$framework = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319'
$compiler = Join-Path $framework 'csc.exe'
$sources = @((Join-Path $PSScriptRoot 'UiaBridge.cs'), (Join-Path $PSScriptRoot 'UiaBridge.navigation.part.cs'), (Join-Path $PSScriptRoot 'UiaBridge.process.part.cs'), (Join-Path $PSScriptRoot 'UiaBridge.metadata.part.cs'), (Join-Path $PSScriptRoot 'UiaBridge.fields.part.cs'))
$output = Join-Path $PSScriptRoot 'UiaBridge.dll'
$references = @(
    (Join-Path $framework 'WPF\UIAutomationClient.dll'),
    (Join-Path $framework 'WPF\UIAutomationTypes.dll'),
    (Join-Path $framework 'System.dll'),
    (Join-Path $framework 'Accessibility.dll')
)
foreach ($path in @($compiler) + $sources + $references) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Missing framework/source dependency: $path" }
}
$arguments = @('/nologo', '/target:library', '/platform:anycpu', '/optimize+', ('/out:"' + $output + '"'))
foreach ($reference in $references) { $arguments += '/reference:"' + $reference + '"' }
foreach ($source in $sources) { $arguments += '"' + $source + '"' }
$stdout = Join-Path $PSScriptRoot 'UiaBridge.build.stdout.log'
$stderr = Join-Path $PSScriptRoot 'UiaBridge.build.stderr.log'
$build = Start-Process -FilePath $compiler -ArgumentList $arguments -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput $stdout -RedirectStandardError $stderr
# Windows PowerShell 5.1 may otherwise lose a short-lived child's exit status.
# Acquire and retain its native handle before waiting for completion.
$compilerHandle = $build.Handle
if (-not $build.WaitForExit(60000)) {
    $build.Kill()
    throw 'UiaBridge compile exceeded 60 seconds.'
}
$build.Refresh()
$compilerExitCode = $build.ExitCode
if ($null -eq $compilerExitCode) {
    throw 'UiaBridge compiler exited, but Windows did not expose its exit status. Inspect the build logs; compilation success is unconfirmed.'
}
if ($compilerExitCode -ne 0) {
    Get-Content -LiteralPath $stdout -ErrorAction SilentlyContinue
    Get-Content -LiteralPath $stderr -ErrorAction SilentlyContinue
    throw "UiaBridge compile failed with exit code $compilerExitCode."
}
Get-Item -LiteralPath $output | Select-Object FullName, Length
