param(
    [string]$Python = "python",
    [string]$OutputDir = "",
    [bool]$WriteCsv = $true,
    [string[]]$Indices = @()
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$FinancialRagDir = Split-Path -Parent $ScriptDir
$TopFlopScript = Join-Path $FinancialRagDir "fetch_finanzen_topflop.py"

if (-not $OutputDir) {
    $OutputDir = Join-Path $FinancialRagDir "csv_update\TOP_LIST"
}

$LogDir = Join-Path $OutputDir "logs"
New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

$Timestamp = Get-Date -Format "yyyyMMdd_HHmm"
$OutputPath = Join-Path $OutputDir "top_liste_$Timestamp.xlsx"
$LogPath = Join-Path $LogDir "top_liste_$Timestamp.log"
$LatestXlsx = Join-Path $OutputDir "top_liste_latest.xlsx"
$LatestCsv = Join-Path $OutputDir "top_liste_latest.csv"

$Arguments = @($TopFlopScript, "--output", $OutputPath)
if ($WriteCsv) {
    $Arguments += "--csv"
}
if ($Indices.Count -gt 0) {
    $Arguments += "--indices"
    $Arguments += $Indices
}

Set-Location $FinancialRagDir

Write-Host "Financial RAG : $FinancialRagDir"
Write-Host "Python        : $Python"
Write-Host "Output XLSX   : $OutputPath"
Write-Host "Log           : $LogPath"

$Output = & $Python @Arguments 2>&1
$ExitCode = $LASTEXITCODE
$Output | Tee-Object -FilePath $LogPath

if ($ExitCode -ne 0) {
    throw "fetch_finanzen_topflop.py failed with exit code $ExitCode. See log: $LogPath"
}

if (Test-Path $OutputPath) {
    Copy-Item -Path $OutputPath -Destination $LatestXlsx -Force
    Write-Host "Updated latest XLSX: $LatestXlsx"
}

$CsvPath = [System.IO.Path]::ChangeExtension($OutputPath, ".csv")
if ($WriteCsv -and (Test-Path $CsvPath)) {
    Copy-Item -Path $CsvPath -Destination $LatestCsv -Force
    Write-Host "Updated latest CSV : $LatestCsv"
}

Write-Host "Done."
