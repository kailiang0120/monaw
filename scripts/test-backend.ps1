param(
    [ValidateSet("all", "api", "runtime", "policy", "sandbox", "skills", "memory", "integrations")]
    [string]$Group = "all",
    [int]$TimeoutSeconds = 900
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$backendRoot = Join-Path $repoRoot "backend"

$pytestArgs = @("run", "--locked", "pytest")
if ($Group -ne "all") {
    $pytestArgs += @("-m", $Group)
}

$runId = [guid]::NewGuid().ToString("N")
$stdoutPath = Join-Path ([System.IO.Path]::GetTempPath()) "monaw-pytest-$runId.stdout.log"
$stderrPath = Join-Path ([System.IO.Path]::GetTempPath()) "monaw-pytest-$runId.stderr.log"
$process = Start-Process `
    -FilePath "uv" `
    -ArgumentList $pytestArgs `
    -WorkingDirectory $backendRoot `
    -NoNewWindow `
    -PassThru `
    -RedirectStandardOutput $stdoutPath `
    -RedirectStandardError $stderrPath

try {
    if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
        Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        throw "Backend '$Group' tests exceeded the ${TimeoutSeconds}s suite timeout."
    }

    if (Test-Path $stdoutPath) {
        Get-Content $stdoutPath
    }
    if (Test-Path $stderrPath) {
        Get-Content $stderrPath
    }
    if ($process.ExitCode -ne 0) {
        exit $process.ExitCode
    }
}
finally {
    Remove-Item -LiteralPath $stdoutPath -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $stderrPath -Force -ErrorAction SilentlyContinue
}
