param(
    [string]$OutputPath = "",
    [switch]$SkipTests,
    # Packaged app to measure (built by scripts/package-windows.ps1 -Unpacked). Skipped when absent.
    [string]$PackagedApp = "",
    [int]$IdleSeconds = 60
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$frontendRoot = Join-Path $repoRoot "frontend"
if (-not $PackagedApp) {
    $PackagedApp = Join-Path $frontendRoot "releasewin-unpackedMonaw.exe"
}

function Measure-PackagedApp {
    param([string]$App, [int]$IdleSeconds)

    # A throwaway home with no development tools on PATH; known folders resolve inside it.
    $home_ = Join-Path ([System.IO.Path]::GetTempPath()) ("monaw-baseline-" + [guid]::NewGuid().ToString("N").Substring(0, 8))
    foreach ($folder in "temp", "AppData\Roaming", "AppData\Local") {
        New-Item -ItemType Directory -Force -Path (Join-Path $home_ $folder) | Out-Null
    }
    $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
    $listener.Start()
    $port = $listener.LocalEndpoint.Port
    $listener.Stop()

    $launch = {
        $info = [System.Diagnostics.ProcessStartInfo]::new($App)
        $info.UseShellExecute = $false
        $info.Environment.Clear()
        $values = @{
            SystemRoot = $env:SystemRoot; windir = $env:SystemRoot
            PATH = "$env:SystemRoot\System32;$env:SystemRoot;$env:SystemRoot\System32\WindowsPowerShell\v1.0"
            TEMP = (Join-Path $home_ "temp"); TMP = (Join-Path $home_ "temp"); USERPROFILE = $home_
            APPDATA = (Join-Path $home_ "AppData\Roaming"); LOCALAPPDATA = (Join-Path $home_ "AppData\Local")
            MONAW_HOME = $home_; MONAW_BACKEND_PORT = "$port"
        }
        foreach ($key in $values.Keys) { $info.Environment[$key] = $values[$key] }
        $watch = [System.Diagnostics.Stopwatch]::StartNew()
        $process = [System.Diagnostics.Process]::Start($info)
        $seconds = $null
        while ($watch.Elapsed.TotalSeconds -lt 90 -and -not $process.HasExited) {
            try {
                if ((Invoke-WebRequest -Uri "http://127.0.0.1:$port/health" -UseBasicParsing -TimeoutSec 2).StatusCode -eq 200) {
                    $seconds = [Math]::Round($watch.Elapsed.TotalSeconds, 2)
                    break
                }
            }
            catch { Start-Sleep -Milliseconds 100 }
        }
        [pscustomobject]@{ Process = $process; Seconds = $seconds }
    }
    $stop = {
        param($process)
        & taskkill /pid $process.Id /t /f | Out-Null
        Start-Sleep -Seconds 2
    }

    $log = Join-Path $home_ "runtime\backend.log"
    $requestLines = { if (Test-Path $log) { @(Select-String -Path $log -Pattern '"(GET|POST|PUT|PATCH|DELETE) /api/').Count } else { 0 } }
    try {
        $cold = & $launch
        $idleRequests = $null
        $databaseBytes = $null
        if ($null -ne $cold.Seconds) {
            # Let the window finish its startup requests, then observe a quiet period.
            Start-Sleep -Seconds 10
            $before = & $requestLines
            Start-Sleep -Seconds $IdleSeconds
            $idleRequests = [Math]::Round(((& $requestLines) - $before) * 60.0 / $IdleSeconds, 1)
            $database = Join-Path $home_ "runtime\agent.db"
            if (Test-Path $database) { $databaseBytes = (Get-Item $database).Length }
        }
        & $stop $cold.Process
        $warm = & $launch
        & $stop $warm.Process
        [ordered]@{
            app = $App
            cold_start_to_healthy_backend_seconds = $cold.Seconds
            warm_start_to_healthy_backend_seconds = $warm.Seconds
            idle_api_requests_per_minute = $idleRequests
            idle_observation_seconds = $IdleSeconds
            fresh_database_bytes = $databaseBytes
            method = "Launch with only Windows folders on PATH in a new Monaw home; time from process start to /health 200. Idle requests are backend access-log lines for /api/ over the observation window after a 10 second settle. Cold is the first launch in the new home; warm relaunches the same home."
        }
    }
    finally {
        if ($home_.StartsWith([System.IO.Path]::GetTempPath()) -and $home_ -like "*monaw-baseline-*") {
            Remove-Item -Recurse -Force $home_ -ErrorAction SilentlyContinue
        }
    }
}

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
        python = (& uv run --project (Join-Path $repoRoot "backend") --locked python --version 2>&1 | Out-String).Trim()
        node = (& node --version 2>&1 | Out-String).Trim()
    }
    application = [ordered]@{
        commit = (& git -C $repoRoot rev-parse HEAD 2>&1 | Out-String).Trim()
        working_tree_clean = -not [bool](& git -C $repoRoot status --porcelain 2>&1 | Out-String).Trim()
        electron = (Get-Content (Join-Path $frontendRoot "node_moduleselectronpackage.json") -Raw | ConvertFrom-Json).version
    }
    backend_test_seconds = $null
    frontend_test_seconds = $null
    frontend_typecheck_seconds = $null
    frontend_build_seconds = $null
    frontend_bundle_bytes = $null
    packaged = $null
    representative_database_bytes = $null
    notes = @(
        "representative_database_bytes requires a provider-independent fixture conversation in the packaged app",
        "long conversation rendering and stop-to-worker quiescence are not measured yet"
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

if (Test-Path $PackagedApp) {
    $result.packaged = Measure-PackagedApp -App $PackagedApp -IdleSeconds $IdleSeconds
}
else {
    $result.notes += "packaged measurements skipped: $PackagedApp not found"
}

$result | ConvertTo-Json -Depth 5 | Set-Content -Path $resolvedOutput -Encoding utf8
Write-Output "Baseline written to $resolvedOutput"
