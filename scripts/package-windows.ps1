param(
    # Build the unpacked app only (release\win-unpacked), without the installer.
    [switch]$Unpacked,
    # Reuse an existing backend\dist\monaw-backend instead of rebuilding it.
    [switch]$SkipBackend
)

# Builds the Windows package: the frozen backend (PyInstaller), then the Electron
# app and its NSIS installer (electron-builder). See docs/packaging.md.

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot

if (-not $SkipBackend) {
    Push-Location (Join-Path $repoRoot "backend")
    try {
        uv run --locked --group packaging pyinstaller packaging/monaw-backend.spec --noconfirm
        if ($LASTEXITCODE -ne 0) {
            exit $LASTEXITCODE
        }
    }
    finally {
        Pop-Location
    }
}

$frozenBackend = Join-Path $repoRoot "backend\dist\monaw-backend\monaw-backend.exe"
if (-not (Test-Path $frozenBackend)) {
    throw "Frozen backend not found at $frozenBackend. Run without -SkipBackend."
}

# frontend\build\THIRD_PARTY_NOTICES.txt, which the installer ships.
Push-Location (Join-Path $repoRoot "backend")
try {
    uv run --locked --group packaging python ../scripts/generate-third-party-notices.py
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
}
finally {
    Pop-Location
}

Push-Location (Join-Path $repoRoot "frontend")
try {
    if ($Unpacked) {
        npm run package:win:dir
    }
    else {
        npm run package:win
    }
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
}
finally {
    Pop-Location
}

if (-not $Unpacked) {
    Push-Location (Join-Path $repoRoot "backend")
    try {
        uv run --locked --group packaging python ../scripts/verify-update-metadata.py
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    }
    finally {
        Pop-Location
    }
}
