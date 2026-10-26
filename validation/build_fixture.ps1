$ErrorActionPreference = 'Stop'
$compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
$source = Join-Path $PSScriptRoot 'CsvRouteFixture.cs'
$output = Join-Path $PSScriptRoot 'fullroutebin\CsvRouteFixture.exe'
& $compiler /nologo /target:winexe /platform:x64 "/out:$output" /reference:System.Windows.Forms.dll /reference:System.Drawing.dll /reference:System.Web.Extensions.dll $source
if ($LASTEXITCODE -ne 0) { throw 'Synthetic fixture compilation failed.' }
