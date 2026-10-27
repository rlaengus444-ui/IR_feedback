# Private Excel automation instance. The selected source is a temporary copy.
$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class IRExcelProcessId {
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint processId);
}
'@
$excel = $null
$book = $null
$sheet = $null
$owned = $false
$tempColumn = $null
$actualColumn = $null
$recommendationColumn = $null
$layerColumn = $null
$sliceColumns = @{}
$targetCell = $null
$firstRow = 0
$lastRow = 0
$maxColumn = 0
$privatePath = $null
$sheetName = $null
$targetAddress = $null

function Send-Reply($message) {
    [Console]::Out.WriteLine(($message | ConvertTo-Json -Compress -Depth 5))
    [Console]::Out.Flush()
}

function Clear-Inputs {
    foreach ($column in (@($tempColumn, $actualColumn, $layerColumn) + @($sliceColumns.Values))) {
        if ($null -ne $column) {
            [void]$sheet.Range($sheet.Cells.Item($firstRow, $column), $sheet.Cells.Item($lastRow, $column)).ClearContents()
        }
    }
}

try {
    while (($line = [Console]::In.ReadLine()) -ne $null) {
        try {
            $request = $line | ConvertFrom-Json
            switch ($request.op) {
                'init' {
                    if ($null -ne $excel) { throw 'Excel already initialized.' }
                    $priorPids = @(Get-Process -Name EXCEL -ErrorAction SilentlyContinue | ForEach-Object { $_.Id })
                    try { $excel = New-Object -ComObject Excel.Application }
                    catch { throw 'Cannot start Microsoft Excel. Verify that Excel is installed.' }
                    [uint32]$excelPid = 0
                    [void][IRExcelProcessId]::GetWindowThreadProcessId([IntPtr]([long]$excel.Hwnd), [ref]$excelPid)
                    if ($excelPid -eq 0 -or $priorPids -contains $excelPid -or $excel.Visible -or $excel.Workbooks.Count -ne 0) {
                        throw 'Cannot verify a new isolated Excel instance.'
                    }
                    $owned = $true
                    Send-Reply @{ event = 'excel_started'; pid = $excelPid }
                    $excel.Visible = $false
                    $excel.DisplayAlerts = $false
                    $excel.EnableEvents = $false
                    $excel.AskToUpdateLinks = $false
                    $excel.ScreenUpdating = $false
                    $excel.AutomationSecurity = 3 # msoAutomationSecurityForceDisable
                    # Excel may reject Calculation changes before its first workbook.
                    $bootstrap = $excel.Workbooks.Add()
                    $excel.Calculation = -4135 # xlCalculationManual
                    $excel.CalculateBeforeSave = $false
                    $privatePath = [string]$request.path
                    $sheetName = [string]$request.sheet
                    $targetAddress = [string]$request.target
                    $book = $excel.Workbooks.Open($privatePath, 0, $true)
                    $bootstrap.Close($false)
                    if (-not $book.ReadOnly -or $excel.Workbooks.Count -ne 1) { throw 'Cannot verify isolated, read-only workbook.' }
                    $sheet = $book.Worksheets.Item($sheetName)
                    $firstRow = [int]$request.first
                    $lastRow = [int]$request.last
                    $maxColumn = [int]$request.max_column
                    $tempColumn = [int]$sheet.Range([string]$request.temperature).Column
                    $actualColumn = [int]$sheet.Range([string]$request.actual).Column
                    $recommendationColumn = [int]$sheet.Range([string]$request.recommendation).Column
                    $targetCell = $sheet.Range($targetAddress)
                    if ($request.layer) { $layerColumn = [int]$sheet.Range([string]$request.layer).Column }
                    foreach ($property in $request.slice_inputs.PSObject.Properties) {
                        $sliceColumns[$property.Name] = [int]$sheet.Range([string]$property.Value).Column
                    }
                    Clear-Inputs
                    Send-Reply @{ ok = $true }
                }
                'reset' {
                    # Reopen the private unsaved copy to restore all frozen row formulas.
                    # Keep one blank workbook open so Excel retains manual calculation.
                    $bootstrap = $excel.Workbooks.Add()
                    $book.Close($false)
                    $book = $excel.Workbooks.Open($privatePath, 0, $true)
                    $bootstrap.Close($false)
                    if (-not $book.ReadOnly -or $excel.Workbooks.Count -ne 1) { throw 'Cannot verify isolated, read-only workbook.' }
                    $sheet = $book.Worksheets.Item($sheetName)
                    $targetCell = $sheet.Range($targetAddress)
                    Clear-Inputs
                    Send-Reply @{ ok = $true }
                }
                'step' {
                    $row = $firstRow + [int]$request.index
                    if ($row -lt $firstRow -or $row -gt $lastRow) { throw 'Layer formula range exceeded.' }
                    $output = $sheet.Cells.Item($row, $recommendationColumn)
                    if (-not $output.HasFormula) { throw "No IR formula on row: $row" }
                    $targetCell.Value2 = [double]$request.target
                    $sheet.Cells.Item($row, $tempColumn).Value2 = [double]$request.temperature
                    $sheet.Cells.Item($row, $actualColumn).Value2 = [double]$request.actual
                    if ($null -ne $layerColumn) { $sheet.Cells.Item($row, $layerColumn).Value2 = [int]$request.layer }
                    foreach ($name in $sliceColumns.Keys) {
                        $cell = $sheet.Cells.Item($row, $sliceColumns[$name])
                        $value = $request.image.PSObject.Properties[$name].Value
                        if ($null -eq $value) { [void]$cell.ClearContents() }
                        else { $cell.Value2 = [double]$value }
                    }
                    $excel.Calculate()
                    $value = $output.Value2
                    if (($value -isnot [double]) -and ($value -isnot [int]) -and ($value -isnot [decimal])) {
                        throw 'IR formula did not return a number.'
                    }
                    # Preserve every intermediate formula result for this layer.
                    # Later target changes must not rewrite earlier decisions.
                    $completedRow = $sheet.Range($sheet.Cells.Item($row, 1), $sheet.Cells.Item($row, $maxColumn))
                    $completedRow.Value2 = $completedRow.Value2
                    Send-Reply @{ ok = $true; value = $value }
                }
                'close' { Send-Reply @{ ok = $true }; break }
                default { throw 'Unsupported command.' }
            }
            if ($request.op -eq 'close') { break }
        }
        catch {
            Send-Reply @{ error = $_.Exception.Message }
            break
        }
    }
}
finally {
    if ($owned) {
        if ($null -ne $book) { try { $book.Close($false) } catch {} }
        if ($null -ne $excel) { try { $excel.Quit() } catch {} }
    }
    foreach ($object in @($sheet, $book, $excel)) {
        if ($null -ne $object) { try { [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($object) } catch {} }
    }
}
