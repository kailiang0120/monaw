param(
    [string]$OutputPath = "",
    [switch]$SkipTests
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$frontendRoot = Join-Path $repoRoot "frontend"

if (-not $OutputPath) {
    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $OutputPath = Join-Path $repoRoot ".artifacts/baselines/$stamp.json"
}

$resolvedOutput = [System.IO.Path]::GetFullPath($OutputPath)
$outputDirectory = Split-Path -Parent $resolvedOutput
New-Item -ItemType Directory -Force -Path $outputDirectory | Out-Null

$result = [ordered]@{
    measured_at_utc = (Get-Date).ToUniversalTime().ToString("o")
    machine = [ordered]@{
        os = [System.Environment]::OSVersion.VersionString
        processor_count = [System.Environment]::ProcessorCount
        python = (& python --version 2>&1 | Out-String).Trim()
        node = (& node --version 2>&1 | Out-String).Trim()
    }
    backend_test_seconds = $null
    frontend_test_seconds = $null
    frontend_typecheck_seconds = $null
    frontend_build_seconds = $null
    frontend_bundle_bytes = $null
    startup_seconds = $null
    idle_api_requests_per_minute = $null
    representative_database_bytes = $null
    notes = @(
        "startup_seconds requires a packaged-app startup harness",
        "idle_api_requests_per_minute requires application network instrumentation",
        "representative_database_bytes requires a provider-independent fixture conversation"
    )
}

if (-not $SkipTests) {
    $duration = Measure-Command {
        & (Join-Path $PSScriptRoot "test-backend.ps1") -Group all
        if ($LASTEXITCODE -ne 0) { throw "Backend tests failed." }
    }
    $result.backend_test_seconds = [Math]::Round($duration.TotalSeconds, 3)

    Push-Location $frontendRoot
    try {
        $duration = Measure-Command {
            npm test
            if ($LASTEXITCODE -ne 0) { throw "Frontend tests failed." }
        }
        $result.frontend_test_seconds = [Math]::Round($duration.TotalSeconds, 3)

        $duration = Measure-Command {
            npm run typecheck
            if ($LASTEXITCODE -ne 0) { throw "Frontend typecheck failed." }
        }
        $result.frontend_typecheck_seconds = [Math]::Round($duration.TotalSeconds, 3)

        $duration = Measure-Command {
            npm run build:renderer
            if ($LASTEXITCODE -ne 0) { throw "Frontend build failed." }
        }
        $result.frontend_build_seconds = [Math]::Round($duration.TotalSeconds, 3)
    }
    finally {
        Pop-Location
    }
}

$distPath = Join-Path $frontendRoot "dist"
if (Test-Path $distPath) {
    $result.frontend_bundle_bytes = (
        Get-ChildItem -Path $distPath -File -Recurse |
            Measure-Object -Property Length -Sum
    ).Sum
}

$result | ConvertTo-Json -Depth 5 | Set-Content -Path $resolvedOutput -Encoding utf8
Write-Output "Baseline written to $resolvedOutput"
