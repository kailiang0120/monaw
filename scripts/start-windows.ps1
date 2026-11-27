$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
$frontendDir = Join-Path $repoRoot 'frontend'
. (Join-Path $PSScriptRoot 'node-prerequisites.ps1')
. (Join-Path $PSScriptRoot 'runtime-prerequisites.ps1')
$viteConfigPath = Join-Path $frontendDir 'vite.config.ts'
$monawHome = if ($env:MONAW_HOME) {
  $env:MONAW_HOME
} elseif ($env:AGENT_HOME) {
  $env:AGENT_HOME
} else {
  Join-Path $HOME '.monaw'
}
$runtimeDir = if ($env:AGENT_RUNTIME_DIR) {
  $env:AGENT_RUNTIME_DIR
} else {
  Join-Path $monawHome 'runtime'
}
$launcherLogDir = Join-Path $runtimeDir 'launcher'

# The source renderer, CORS policy, and Electron navigation policy use this fixed port.
function Get-VitePort {
  if (-not (Test-Path $viteConfigPath)) {
    return 5275
  }

  $config = Get-Content -Raw -Path $viteConfigPath
  $match = [regex]::Match($config, 'port\s*:\s*(\d+)')
  if ($match.Success -and [int]$match.Groups[1].Value -ne 5275) {
    throw 'The source renderer requires Vite port 5275. Restore that port in frontend/vite.config.ts; changing only Vite does not update Electron or CORS.'
  }
  return 5275
}

function Test-Endpoint {
  param([Parameter(Mandatory = $true)] [string] $Url)

  try {
    $response = Invoke-WebRequest -UseBasicParsing -Uri $Url -TimeoutSec 2
    return [int]$response.StatusCode -ge 200 -and [int]$response.StatusCode -lt 500
  } catch {
    return $false
  }
}

function Wait-ForEndpoint {
  param(
    [Parameter(Mandatory = $true)] [string] $Name,
    [Parameter(Mandatory = $true)] [string] $Url,
    [int] $TimeoutSeconds = 60,
    [System.Diagnostics.Process] $Process = $null
  )

  $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
  while ((Get-Date) -lt $deadline) {
    if ($Process -and $Process.HasExited) {
      throw "$Name process exited before it became ready."
    }

    if (Test-Endpoint -Url $Url) {
      return
    }

    Start-Sleep -Milliseconds 750
  }

  throw "$Name did not become ready within $TimeoutSeconds seconds."
}

function Test-LoopbackHost {
  param([Parameter(Mandatory = $true)] [string] $HostName)

  $normalized = $HostName.Trim().ToLowerInvariant()
  return @('127.0.0.1', 'localhost', '::1') -contains $normalized
}

function Assert-SafeBackendHost {
  param([Parameter(Mandatory = $true)] [string] $HostName)

  if (Test-LoopbackHost -HostName $HostName) {
    return
  }
  if ($env:MONAW_ALLOW_UNSAFE_BACKEND_HOST -eq '1') {
    Write-Warning 'MONAW_ALLOW_UNSAFE_BACKEND_HOST=1 is set; non-loopback backend host is allowed for this launch.'
    return
  }
  throw 'Refusing to start with a non-loopback backend host. Use 127.0.0.1, localhost, or ::1.'
}

function Assert-RuntimeDirectory {
  param([Parameter(Mandatory = $true)] [string] $Path)

  New-Item -ItemType Directory -Force -Path $Path | Out-Null
  $item = Get-Item -LiteralPath $Path -Force
  if ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
    throw 'Runtime directory must not be a symlink or junction.'
  }
  $probe = Join-Path $Path ('.startup-check-{0}.tmp' -f ([guid]::NewGuid().ToString('N')))
  try {
    Set-Content -LiteralPath $probe -Value 'ok' -Encoding UTF8
    if ((Get-Content -Raw -LiteralPath $probe) -notmatch '^ok') {
      throw 'Runtime directory write verification failed.'
    }
  } finally {
    Remove-Item -LiteralPath $probe -Force -ErrorAction SilentlyContinue
  }
}

function Start-NpmScriptHidden {
  param(
    [Parameter(Mandatory = $true)] [string] $ScriptName,
    [Parameter(Mandatory = $true)] [string] $LogPath
  )

  $npmCommand = Get-Command npm.cmd -ErrorAction SilentlyContinue
  $npm = if ($npmCommand) { $npmCommand.Source } else { $null }
  if (-not $npm) {
    $npm = (Get-Command npm -ErrorAction Stop).Source
  }

  $extraArgs = if ($ScriptName -eq 'dev:vite') { ' -- --port {0} --strictPort' -f $vitePort } else { '' }
  $command = 'cd /d "{0}" && "{1}" run {2}{3} > "{4}" 2>&1' -f $frontendDir, $npm, $ScriptName, $extraArgs, $LogPath
  return Start-Process -FilePath $env:ComSpec -ArgumentList @('/d', '/s', '/c', $command) -WindowStyle Hidden -PassThru
}

function Hide-ConsoleIfExplorerLaunched {
  try {
    $current = Get-CimInstance Win32_Process -Filter "ProcessId = $PID"
    $parent = Get-CimInstance Win32_Process -Filter "ProcessId = $($current.ParentProcessId)"
    $grandParent = if ($parent) {
      Get-CimInstance Win32_Process -Filter "ProcessId = $($parent.ParentProcessId)" -ErrorAction SilentlyContinue
    } else {
      $null
    }

    if (-not $grandParent -or $grandParent.Name -ne 'explorer.exe') {
      return
    }

    Add-Type -Name ConsoleWindow -Namespace Native -MemberDefinition @'
[DllImport("kernel32.dll")]
public static extern IntPtr GetConsoleWindow();
[DllImport("user32.dll")]
public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
'@
    $consoleHandle = [Native.ConsoleWindow]::GetConsoleWindow()
    if ($consoleHandle -ne [IntPtr]::Zero) {
      [Native.ConsoleWindow]::ShowWindow($consoleHandle, 0) | Out-Null
    }
  } catch {
    # Hiding the launcher console is best-effort; startup should not fail because of it.
  }
}

$viteLog = Join-Path $launcherLogDir 'vite.log'
$electronLog = Join-Path $launcherLogDir 'electron.log'

$startedVite = $false
$viteProcess = $null
$electronProcess = $null

try {
  Write-Host 'Checking source checkout prerequisites...'
  Update-MonawProcessPath
  $null = Assert-MonawNodeRuntime
  $uv = Get-MonawUvCommand
  if (-not $uv) {
    throw 'uv is required. Run setup.bat first, or set AGENT_UV_PATH to the full path of uv.exe.'
  }
  Write-Host "Using uv: $uv"
  & $uv --version
  if ($LASTEXITCODE -ne 0) { throw 'uv could not run. Repair its installation or AGENT_UV_PATH, then run setup.bat.' }
  # Keep Electron on the exact executable checked above, including custom installations.
  $env:AGENT_UV_PATH = $uv
  foreach ($requiredPath in @('backend/.venv/Scripts/python.exe', 'frontend/node_modules/.bin/vite.cmd')) {
    if (-not (Test-Path -LiteralPath (Join-Path $repoRoot $requiredPath) -PathType Leaf)) {
      throw "Missing $requiredPath. Run setup.bat before starting Monaw."
    }
  }
  & node (Join-Path $frontendDir 'scripts/check-electron-install.cjs')
  if ($LASTEXITCODE -ne 0) { throw 'The Electron desktop executable is missing. Run setup.bat to reinstall frontend dependencies.' }
  if ($env:MONAW_RENDERER_MODE -eq 'bundle' -and -not (Test-Path -LiteralPath (Join-Path $frontendDir 'dist/index.html'))) {
    throw 'MONAW_RENDERER_MODE=bundle requires a renderer build. Run npm run build:renderer in frontend, or remove that override.'
  }

  $backendConfig = Get-MonawBackendConfig
  $backendHost = $backendConfig.HostName
  $backendPort = $backendConfig.Port
  $vitePort = Get-VitePort
  Assert-SafeBackendHost -HostName $backendHost
  Assert-RuntimeDirectory -Path $runtimeDir
  New-Item -ItemType Directory -Force -Path $launcherLogDir | Out-Null
  $viteUrl = "http://localhost:$vitePort"
  $healthHost = if ($backendHost -eq '::1') { '[::1]' } else { $backendHost }
  $backendHealthUrl = "http://${healthHost}:$backendPort/health"
  Write-Host "Backend: $backendHealthUrl"
  Write-Host "Logs: $launcherLogDir"

  if ($env:MONAW_RENDERER_MODE -eq 'bundle') {
    Write-Host 'Using the built renderer; Vite is not needed.'
  } elseif (Test-Endpoint -Url $viteUrl) {
    Write-Host "Vite is already running at $viteUrl."
  } else {
    Write-Host "Starting Vite on $viteUrl..."
    $viteProcess = Start-NpmScriptHidden -ScriptName 'dev:vite' -LogPath $viteLog
    $startedVite = $true
    Wait-ForEndpoint -Name 'Vite' -Url $viteUrl -TimeoutSeconds 60 -Process $viteProcess
  }

  Write-Host "Starting Electron..."
  $electronProcess = Start-NpmScriptHidden -ScriptName 'dev:electron' -LogPath $electronLog
  Wait-ForEndpoint -Name 'Backend' -Url $backendHealthUrl -TimeoutSeconds 90 -Process $electronProcess

  Write-Host "Monaw Agent started successfully. Logs: $launcherLogDir"
  Hide-ConsoleIfExplorerLaunched
  exit 0
} catch {
  Write-Host "[ERROR] $($_.Exception.Message)"
  Write-Host "Check launcher logs in: $launcherLogDir"

  # Clean up only the process trees created by this attempt; they may already have exited.
  $ErrorActionPreference = 'Continue'
  if ($electronProcess -and -not $electronProcess.HasExited) {
    & taskkill.exe /PID $electronProcess.Id /T /F 2>&1 | Out-Null
  }
  if ($startedVite -and $viteProcess -and -not $viteProcess.HasExited) {
    & taskkill.exe /PID $viteProcess.Id /T /F 2>&1 | Out-Null
  }

  exit 1
}
