$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
$frontendDir = Join-Path $repoRoot 'frontend'
$runtimeConfigPath = Join-Path $frontendDir 'config\runtime.json'
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

New-Item -ItemType Directory -Force -Path $launcherLogDir | Out-Null

function Get-JsonValueOrDefault {
  param(
    [Parameter(Mandatory = $true)] [object] $Object,
    [Parameter(Mandatory = $true)] [string] $Property,
    [Parameter(Mandatory = $true)] [object] $Default
  )

  if ($null -ne $Object -and $Object.PSObject.Properties.Name -contains $Property) {
    return $Object.$Property
  }
  return $Default
}

function Get-VitePort {
  if (-not (Test-Path $viteConfigPath)) {
    return 5275
  }

  $config = Get-Content -Raw -Path $viteConfigPath
  $match = [regex]::Match($config, 'port\s*:\s*(\d+)')
  if ($match.Success) {
    return [int]$match.Groups[1].Value
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
    Set-Content -LiteralPath $probe -Value 'ok' -Encoding utf8NoBOM
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

$runtimeConfig = if (Test-Path $runtimeConfigPath) {
  Get-Content -Raw -Path $runtimeConfigPath | ConvertFrom-Json
} else {
  $null
}

$backendHost = Get-JsonValueOrDefault -Object $runtimeConfig -Property 'backendHost' -Default '127.0.0.1'
$backendPort = Get-JsonValueOrDefault -Object $runtimeConfig -Property 'backendPort' -Default 8420
$vitePort = Get-VitePort

Assert-SafeBackendHost -HostName ([string]$backendHost)
Assert-RuntimeDirectory -Path $runtimeDir

$viteUrl = "http://localhost:$vitePort"
$backendHealthUrl = "http://${backendHost}:$backendPort/health"
$viteLog = Join-Path $launcherLogDir 'vite.log'
$electronLog = Join-Path $launcherLogDir 'electron.log'

$startedVite = $false
$viteProcess = $null
$electronProcess = $null

try {
  if (Test-Endpoint -Url $viteUrl) {
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

  if ($electronProcess -and -not $electronProcess.HasExited) {
    Stop-Process -Id $electronProcess.Id -Force -ErrorAction SilentlyContinue
  }
  if ($startedVite -and $viteProcess -and -not $viteProcess.HasExited) {
    Stop-Process -Id $viteProcess.Id -Force -ErrorAction SilentlyContinue
  }

  exit 1
}
