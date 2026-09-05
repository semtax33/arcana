param(
    [ValidateSet("unit", "semantic", "integration", "data-heavy", "fast")]
    [string]$Tier = "fast"
)

$python = Join-Path $PSScriptRoot "..\.venv-llama\Scripts\python.exe"
$testRoot = Join-Path $PSScriptRoot "..\tests"
$allFiles = @(Get-ChildItem -LiteralPath $testRoot -Filter "test_*.py" | Sort-Object Name)
$dataHeavyNames = @(
    "test_benchmark_download.py",
    "test_factor_lab_snapshot_coverage.py",
    "test_refresh_workflow.py",
    "test_sec_filings_download.py",
    "test_yfinance_price_elt.py"
)
$semanticTokens = @("semantic", "mapping", "normalizer", "statement_period", "business_info", "comment_extraction", "unit_scale")
$integrationTokens = @("workflow", "pipeline", "loader", "service", "query", "mcp_server", "source_storage")

function Test-NameContains([string]$name, [string[]]$tokens) {
    foreach ($token in $tokens) {
        if ($name.Contains($token)) { return $true }
    }
    return $false
}

$selected = switch ($Tier) {
    "data-heavy" { @($allFiles | Where-Object { $dataHeavyNames -contains $_.Name }) }
    "semantic" { @($allFiles | Where-Object { Test-NameContains $_.BaseName $semanticTokens }) }
    "integration" { @($allFiles | Where-Object { ($dataHeavyNames -notcontains $_.Name) -and (Test-NameContains $_.BaseName $integrationTokens) }) }
    "unit" { @($allFiles | Where-Object { ($dataHeavyNames -notcontains $_.Name) -and -not (Test-NameContains $_.BaseName $semanticTokens) -and -not (Test-NameContains $_.BaseName $integrationTokens) }) }
    "fast" { @($allFiles | Where-Object { $dataHeavyNames -notcontains $_.Name }) }
}

if ($selected.Count -eq 0) {
    throw "No tests selected for tier: $Tier"
}

& $python -m pytest @($selected.FullName)
exit $LASTEXITCODE
