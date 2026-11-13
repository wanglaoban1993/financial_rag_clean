param(
    [string]$Folder = "",
    [int]$MaxJobs = 8,
    [string]$PythonBin = "python",
    [int]$SleepSeconds = 2,
    [double]$RequestSleepSeconds = 0.2,
    [string]$UpdateTypes = ""
)

$ErrorActionPreference = "Stop"
$Stopwatch = [System.Diagnostics.Stopwatch]::StartNew()

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RootDir = Resolve-Path (Join-Path $ScriptDir "..")

# Resolve "python" to a full path *in this session's context* before spawning
# child processes — Start-Process does its own executable search and can land
# on a different install (e.g. base Anaconda instead of an activated env),
# silently missing packages like yfinance that only exist in the active env.
if ($PythonBin -eq "python") {
    $resolvedPython = (Get-Command python -ErrorAction SilentlyContinue).Source
    if ($resolvedPython) {
        $PythonBin = $resolvedPython
    }
}

if ([string]::IsNullOrWhiteSpace($Folder)) {
    $Folder = Join-Path $RootDir "csv_update\ETF"
}

$Folder = (Resolve-Path $Folder).Path
$LogDir = Join-Path $Folder "_bid_ask_logs"
$TempInputDir = Join-Path $LogDir "_inputs"
New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
New-Item -ItemType Directory -Path $TempInputDir -Force | Out-Null

if ($MaxJobs -lt 1) {
    throw "MaxJobs must be a positive integer, got: $MaxJobs"
}

Write-Host "Root        : $RootDir"
Write-Host "Folder      : $Folder"
Write-Host "Python      : $PythonBin"
Write-Host "Max jobs    : $MaxJobs"
Write-Host "Mode        : Bid/Ask only"
Write-Host "Req sleep   : $RequestSleepSeconds"
Write-Host "Update types: $(if ($UpdateTypes) { $UpdateTypes } else { 'all' })"
Write-Host "Log folder  : $LogDir"
Write-Host ""

$csvFiles = Get-ChildItem -LiteralPath $Folder -Filter "*.csv" |
    Where-Object { $_.BaseName -notlike "*_top" } |
    Sort-Object Name

if (-not $csvFiles) {
    throw "No CSV files found in: $Folder"
}

$processes = New-Object System.Collections.Generic.List[object]
$started = 0

function Quote-ProcessArgument {
    param([string]$Value)
    '"' + ($Value -replace '\\', '\\' -replace '"', '\"') + '"'
}

foreach ($csvFile in $csvFiles) {
    while (($processes | Where-Object { -not $_.Process.HasExited }).Count -ge $MaxJobs) {
        Start-Sleep -Seconds $SleepSeconds
    }

    $started += 1
    $safeName = ($csvFile.BaseName -replace '[^\w.-]', '_')
    $stdoutLog = Join-Path $LogDir "$safeName.out.log"
    $stderrLog = Join-Path $LogDir "$safeName.err.log"
    $tempInput = Join-Path $TempInputDir "$safeName.csv"
    $outputXlsx = Join-Path $Folder "$($csvFile.BaseName).xlsx"
    Copy-Item -LiteralPath $csvFile.FullName -Destination $tempInput -Force

    Write-Host "[$started] Starting bid/ask refresh: $($csvFile.FullName)"
    $argList = @(
        "csv_analyse_watchlist.py",
        (Quote-ProcessArgument $tempInput),
        (Quote-ProcessArgument $outputXlsx),
        "--bid-ask-only",
        "--skip-isin-links",
        "--sleep",
        (Quote-ProcessArgument ([string]$RequestSleepSeconds))
    )
    if ($UpdateTypes) {
        $argList += "--update-types"
        foreach ($updateType in ($UpdateTypes -split ',')) {
            $trimmedType = $updateType.Trim()
            if ($trimmedType) {
                $argList += (Quote-ProcessArgument $trimmedType)
            }
        }
    }
    $args = $argList -join " "

    $process = Start-Process `
        -FilePath $PythonBin `
        -ArgumentList $args `
        -WorkingDirectory $RootDir `
        -NoNewWindow `
        -PassThru `
        -RedirectStandardOutput $stdoutLog `
        -RedirectStandardError $stderrLog

    $processes.Add([pscustomobject]@{
        File = $csvFile.FullName
        TempInput = $tempInput
        Process = $process
        Stdout = $stdoutLog
        Stderr = $stderrLog
    })
}

Write-Host ""
Write-Host "Started $started bid/ask job(s). Waiting for completion..."

$failed = 0
foreach ($job in $processes) {
    $job.Process.WaitForExit()
    $job.Process.Refresh()
    if ($null -eq $job.Process.ExitCode) {
        Write-Host "WARN: Exit code unavailable, checking logs: $($job.File)"
        Write-Host "  stdout: $($job.Stdout)"
        Write-Host "  stderr: $($job.Stderr)"
        continue
    }
    if ($job.Process.ExitCode -ne 0) {
        $failed += 1
        Write-Host "FAILED ($($job.Process.ExitCode)): $($job.File)"
        Write-Host "  stdout: $($job.Stdout)"
        Write-Host "  stderr: $($job.Stderr)"
    }
}

if ($failed -gt 0) {
    throw "$failed job(s) failed."
}

Write-Host "All bid/ask jobs finished."
$Stopwatch.Stop()
Write-Host "Total runtime: $($Stopwatch.Elapsed.ToString('hh\:mm\:ss'))"
