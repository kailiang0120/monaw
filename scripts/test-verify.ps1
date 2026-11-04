param()

# Isolated regression check for verify.ps1's PowerShell success semantics.
# Run with: pwsh -NoProfile -File scripts/test-verify.ps1

$ErrorActionPreference = "Stop"
$sourceVerify = Join-Path $PSScriptRoot "verify.ps1"
$testRoot = Join-Path ([System.IO.Path]::GetTempPath()) "monaw-verify-test-$([guid]::NewGuid().ToString('N'))"
$scriptsDir = Join-Path $testRoot "scripts"
$frontendDir = Join-Path $testRoot "frontend"
$fakeBinDir = Join-Path $testRoot "fake-bin"
$resolvedRoot = [System.IO.Path]::GetFullPath($testRoot)
$tempPrefix = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath()).TrimEnd('\') + '\'

if (-not $resolvedRoot.StartsWith($tempPrefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing to use a test directory outside the system temp folder: $resolvedRoot"
}

New-Item -ItemType Directory -Force $scriptsDir, $frontendDir, $fakeBinDir | Out-Null
Copy-Item -LiteralPath $sourceVerify -Destination (Join-Path $scriptsDir "verify.ps1")

$backendStub = @'
param([string]$Group = "all")
[System.IO.File]::WriteAllText($env:MONAW_VERIFY_BACKEND_MARKER, $Group)
if ($env:MONAW_VERIFY_TEST_MODE -eq "fail") {
    exit 7
}
else {
    if ($null -ne $LASTEXITCODE) {
        throw "Expected LASTEXITCODE to remain null after a successful Start-Process-style backend test."
    }
    Write-Output "Simulated backend test succeeded."
}
'@
Set-Content -LiteralPath (Join-Path $scriptsDir "test-backend.ps1") -Value $backendStub -Encoding UTF8

$npmStub = @'
[System.IO.File]::WriteAllText($env:MONAW_VERIFY_FRONTEND_MARKER, ($args -join " "))
$global:LASTEXITCODE = 0
'@
Set-Content -LiteralPath (Join-Path $fakeBinDir "npm.ps1") -Value $npmStub -Encoding UTF8
Set-Content -LiteralPath (Join-Path $fakeBinDir "npm.cmd") -Value @'
@echo off
echo %* > "%MONAW_VERIFY_FRONTEND_MARKER%"
exit /b 0
'@ -Encoding ASCII

$powerShellPath = (Get-Process -Id $PID).Path
$originalPath = $env:PATH
$originalMode = [Environment]::GetEnvironmentVariable("MONAW_VERIFY_TEST_MODE", "Process")
$originalBackendMarker = [Environment]::GetEnvironmentVariable("MONAW_VERIFY_BACKEND_MARKER", "Process")
$originalFrontendMarker = [Environment]::GetEnvironmentVariable("MONAW_VERIFY_FRONTEND_MARKER", "Process")
$env:PATH = "$fakeBinDir;$originalPath"

function Invoke-IsolatedVerify([string]$Mode) {
    $backendMarker = Join-Path $testRoot "backend-$Mode.marker"
    $frontendMarker = Join-Path $testRoot "frontend-$Mode.marker"
    $stdoutPath = Join-Path $testRoot "verify-$Mode.stdout.log"
    $stderrPath = Join-Path $testRoot "verify-$Mode.stderr.log"
    $env:MONAW_VERIFY_TEST_MODE = $Mode
    $env:MONAW_VERIFY_BACKEND_MARKER = $backendMarker
    $env:MONAW_VERIFY_FRONTEND_MARKER = $frontendMarker
    $verifyPath = Join-Path $scriptsDir "verify.ps1"
    $arguments = "-NoProfile -NonInteractive -File `"$verifyPath`""
    $process = Start-Process -FilePath $powerShellPath -ArgumentList $arguments -WorkingDirectory $testRoot `
        -WindowStyle Hidden -PassThru -Wait -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath
    return @{
        ExitCode = $process.ExitCode
        BackendMarker = $backendMarker
        FrontendMarker = $frontendMarker
        Stdout = if (Test-Path $stdoutPath) { Get-Content -Raw $stdoutPath } else { "" }
        Stderr = if (Test-Path $stderrPath) { Get-Content -Raw $stderrPath } else { "" }
    }
}

try {
    $success = Invoke-IsolatedVerify "success"
    if ($success.ExitCode -ne 0) {
        throw "Successful backend check returned $($success.ExitCode).`n$($success.Stdout)`n$($success.Stderr)"
    }
    if (-not (Test-Path -LiteralPath $success.BackendMarker)) {
        throw "The successful backend stub did not run."
    }
    if (-not (Test-Path -LiteralPath $success.FrontendMarker)) {
        throw "Frontend verification was skipped after a successful backend script with a null LASTEXITCODE."
    }

    $failure = Invoke-IsolatedVerify "fail"
    if ($failure.ExitCode -eq 0) {
        throw "A failing backend check returned success.`n$($failure.Stdout)`n$($failure.Stderr)"
    }
    if (-not (Test-Path -LiteralPath $failure.BackendMarker)) {
        throw "The failing backend stub did not run."
    }
    if (Test-Path -LiteralPath $failure.FrontendMarker) {
        throw "Frontend verification ran after the backend script reported failure."
    }

    Write-Output "verify.ps1 regression passed: null LASTEXITCODE runs frontend; backend failure stops before frontend."
}
finally {
    $env:PATH = $originalPath
    foreach ($name in @("MONAW_VERIFY_TEST_MODE", "MONAW_VERIFY_BACKEND_MARKER", "MONAW_VERIFY_FRONTEND_MARKER")) {
        $original = switch ($name) {
            "MONAW_VERIFY_TEST_MODE" { $originalMode }
            "MONAW_VERIFY_BACKEND_MARKER" { $originalBackendMarker }
            "MONAW_VERIFY_FRONTEND_MARKER" { $originalFrontendMarker }
        }
        [Environment]::SetEnvironmentVariable($name, $original, "Process")
    }
    if (Test-Path -LiteralPath $resolvedRoot) {
        Remove-Item -LiteralPath $resolvedRoot -Recurse -Force
    }
}
