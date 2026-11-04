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
    # test-backend.ps1 launches uv via Start-Process, which does not set
    # LASTEXITCODE on success. Inspect the script's success status instead;
    # in a fresh PowerShell session $null -ne 0 would skip the frontend.
    if (-not $?) {
        exit 1
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
