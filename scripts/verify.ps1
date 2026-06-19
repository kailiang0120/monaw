param(
    [ValidateSet("all", "api", "runtime", "policy", "sandbox", "skills", "memory", "integrations")]
    [string]$BackendGroup = "all",
    [switch]$SkipBackend,
    [switch]$SkipFrontend
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot

if (-not $SkipBackend) {
    & (Join-Path $PSScriptRoot "test-backend.ps1") -Group $BackendGroup
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
}

if (-not $SkipFrontend) {
    Push-Location (Join-Path $repoRoot "frontend")
    try {
        npm run verify
        if ($LASTEXITCODE -ne 0) {
            exit $LASTEXITCODE
        }
    }
    finally {
        Pop-Location
    }
}
