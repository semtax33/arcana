param(
    [int]$RawFactorProcessId = 20948
)

$ErrorActionPreference = 'Stop'

$workspace = 'D:\Programming\python_example\Arcana'
$python = Join-Path $workspace '.venv-llama\Scripts\python.exe'
$factorStatus = Join-Path $workspace 'data-lake\meta\kr_financial_normalization_factor_rebuild_status.json'
$factorStderr = Join-Path $workspace 'data-lake\meta\kr_financial_normalization_factor_rebuild_resume_20260903_035559.stderr.log'
$targets = Join-Path $workspace 'data-lake\meta\kr_financial_normalization_factor_targets.csv'
$expectedCount = 2578
$started = Get-Date -Format 'yyyyMMdd_HHmmss'

Set-Location -LiteralPath $workspace

while ($true) {
    $status = Get-Content -Raw -LiteralPath $factorStatus | ConvertFrom-Json
    $counts = @{
        annual = @($status.completed.annual).Count
        quarterly = @($status.completed.quarterly).Count
        ttm = @($status.completed.ttm).Count
    }
    $rawProcess = Get-Process -Id $RawFactorProcessId -ErrorAction SilentlyContinue
    $errorBytes = (Get-Item -LiteralPath $factorStderr).Length

    if ($errorBytes -gt 0) {
        throw "Raw factor rebuild wrote to stderr ($errorBytes bytes); snapshot launch aborted."
    }
    if (($counts.annual -eq $expectedCount) -and
        ($counts.quarterly -eq $expectedCount) -and
        ($counts.ttm -eq $expectedCount)) {
        if ($null -eq $rawProcess) {
            break
        }
    }
    elseif ($null -eq $rawProcess) {
        throw "Raw factor rebuild exited before completion: annual=$($counts.annual), quarterly=$($counts.quarterly), ttm=$($counts.ttm)."
    }

    Write-Output "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] waiting raw factors: annual=$($counts.annual), quarterly=$($counts.quarterly), ttm=$($counts.ttm)"
    Start-Sleep -Seconds 30
}

[xml]$clickHouseConfig = Get-Content -Raw -LiteralPath 'D:\Programming\clickhouse\config.xml'
$env:CLICKHOUSE_PASSWORD = [string]$clickHouseConfig.clickhouse.users.default.password

$processes = @()
foreach ($shard in 0, 1) {
    $snapshotStatus = Join-Path $workspace "data-lake\meta\kr_financial_normalization_snapshot_rebuild_status_shard$shard.json"
    $stdout = Join-Path $workspace "data-lake\meta\kr_financial_normalization_snapshot_rebuild_${started}_shard$shard.stdout.log"
    $stderr = Join-Path $workspace "data-lake\meta\kr_financial_normalization_snapshot_rebuild_${started}_shard$shard.stderr.log"
    $arguments = @(
        '-m', 'scripts.rebuild_affected_factor_snapshots',
        '--market', 'kr',
        '--basis', 'annual',
        '--basis', 'quarterly',
        '--basis', 'ttm',
        '--targets', $targets,
        '--factor-status', $factorStatus,
        '--status', $snapshotStatus,
        '--factor-chunk-size', '64',
        '--factor-shard-index', "$shard",
        '--factor-shard-count', '2',
        '--start-date', '2015-12-31',
        '--end-date', '2026-08-27'
    )
    $processes += Start-Process -FilePath $python -ArgumentList $arguments -WorkingDirectory $workspace -WindowStyle Hidden -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru
    Write-Output "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] snapshot shard $shard started: pid=$($processes[-1].Id)"
}

$processes | Wait-Process
$failed = @($processes | Where-Object { $_.ExitCode -ne 0 })
if ($failed.Count -gt 0) {
    throw "Snapshot rebuild failed: $($failed.Id -join ', ')"
}

Write-Output "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] both snapshot shards completed successfully."
