$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
$backendDir = Join-Path $repoRoot 'backend'
$frontendDir = Join-Path $repoRoot 'frontend'
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
$backendPort = 8420
$runtimeConfigPath = Join-Path $frontendDir 'config\runtime.json'
if (Test-Path $runtimeConfigPath) {
  try {
    $configuredPort = (Get-Content -Raw -Path $runtimeConfigPath | ConvertFrom-Json).backendPort
    if ($configuredPort) { $backendPort = [int]$configuredPort }
  } catch {
    # A malformed runtime.json is start.bat's problem to report; keep the default here.
  }
}
# Vite and the backend.
$monawPorts = @(5275, $backendPort)
$setupLogDir = Join-Path $runtimeDir 'setup'
$setupLog = Join-Path $setupLogDir 'setup.log'

New-Item -ItemType Directory -Force -Path $setupLogDir | Out-Null

function Write-Step {
  param([Parameter(Mandatory = $true)] [string] $Message)
  Write-Host ""
  Write-Host "==> $Message"
}

function Update-ProcessPath {
  $machinePath = [Environment]::GetEnvironmentVariable('Path', 'Machine')
  $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
  $env:Path = @($machinePath, $userPath, $env:Path) -join ';'
}

function Invoke-LoggedCommand {
  param(
    [Parameter(Mandatory = $true)] [string] $FilePath,
    [Parameter(Mandatory = $true)] [string[]] $ArgumentList,
    [string] $WorkingDirectory = $repoRoot
  )

  Write-Host "> $FilePath $($ArgumentList -join ' ')"
  "[$(Get-Date -Format o)] > $FilePath $($ArgumentList -join ' ')" | Add-Content -Path $setupLog

  Push-Location $WorkingDirectory
  $previousErrorActionPreference = $ErrorActionPreference
  try {
    # Native tools such as uv write normal progress messages to stderr.
    # Windows PowerShell turns redirected native stderr into ErrorRecord
    # objects, which must not become terminating errors when the process exits 0.
    $ErrorActionPreference = 'Continue'
    $output = & $FilePath @ArgumentList 2>&1
    $exitCode = if ($null -ne $LASTEXITCODE) { $LASTEXITCODE } else { 0 }
  } finally {
    $ErrorActionPreference = $previousErrorActionPreference
    Pop-Location
  }

  if ($output) {
    $output | Add-Content -Path $setupLog
    $output | ForEach-Object { Write-Host $_ }
  }

  if ($exitCode -ne 0) {
    throw "Command failed with exit code ${exitCode}: $FilePath $($ArgumentList -join ' ')"
  }
}

function Get-SetupAncestry {
  # This setup run and the windows that launched it must never be closed.
  $ids = @()
  $current = $PID
  while ($current -and $ids -notcontains $current) {
    $ids += $current
    $process = Get-CimInstance Win32_Process -Filter "ProcessId = $current" -ErrorAction SilentlyContinue
    $current = if ($process) { $process.ParentProcessId } else { $null }
  }
  return $ids
}

function Get-MonawProcesses {
  $ownedPath = [regex]::Escape($repoRoot)
  $ancestry = Get-SetupAncestry
  return @(
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
      Where-Object {
        $commandLine = $_.CommandLine
        $_.Name -in @('electron.exe', 'node.exe', 'npm.exe', 'uv.exe', 'uvicorn.exe', 'python.exe') -and
        $commandLine -and
        $ancestry -notcontains $_.ProcessId -and
        $commandLine -match $ownedPath
      }
  )
}

function Stop-MonawIfRunning {
  # taskkill writes to stderr when a process is already gone; that is fine here.
  $ErrorActionPreference = 'Continue'

  $processes = @(Get-MonawProcesses)
  if ($processes.Count -gt 0) {
    $names = $processes | ForEach-Object { "$($_.Name) (PID $($_.ProcessId))" }
    Write-Host "Closing the running Monaw: $($names -join ', ')"
    "[$(Get-Date -Format o)] Closing running Monaw: $($names -join ', ')" | Add-Content -Path $setupLog

    # Closing the window first lets Electron stop its backend itself.
    $closedWindow = $false
    foreach ($process in $processes | Where-Object { $_.Name -eq 'electron.exe' }) {
      $window = Get-Process -Id $process.ProcessId -ErrorAction SilentlyContinue
      if ($window -and $window.MainWindowHandle -ne [IntPtr]::Zero) {
        $closedWindow = $window.CloseMainWindow() -or $closedWindow
      }
    }
    if ($closedWindow) {
      $deadline = (Get-Date).AddSeconds(5)
      while ((Get-Date) -lt $deadline -and @(Get-MonawProcesses).Count -gt 0) {
        Start-Sleep -Milliseconds 500
      }
    }
    foreach ($process in @(Get-MonawProcesses)) {
      & taskkill.exe /PID $process.ProcessId /T /F 2>&1 | Out-Null
    }
  }

  # Wait for the sockets to close, but never stop a process Monaw does not own.
  $deadline = (Get-Date).AddSeconds(15)
  do {
    $busyPorts = @(
      Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
        Where-Object { $_.LocalPort -in $monawPorts }
    )
    if ($busyPorts.Count -eq 0) {
      return
    }
    Start-Sleep -Milliseconds 500
  } while ((Get-Date) -lt $deadline)

  $details = $busyPorts | ForEach-Object {
    $owner = Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue
    "$($_.LocalPort) ($(if ($owner) { $owner.ProcessName } else { 'unknown' }), PID $($_.OwningProcess))"
  }
  throw "Monaw's ports are still in use: $($details -join ', '). Close those programs, then run setup.bat again."
}

function Install-WithWinget {
  param(
    [Parameter(Mandatory = $true)] [string] $PackageId,
    [Parameter(Mandatory = $true)] [string] $Name
  )

  $winget = Get-Command winget -ErrorAction SilentlyContinue
  if (-not $winget) {
    throw "Windows Package Manager (winget) is not available. Install $Name manually, then run setup.bat again."
  }

  Write-Step "Installing $Name"
  Invoke-LoggedCommand `
    -FilePath $winget.Source `
    -ArgumentList @(
      'install',
      '--id', $PackageId,
      '--exact',
      '--source', 'winget',
      '--accept-package-agreements',
      '--accept-source-agreements',
      '--disable-interactivity'
    )
  Update-ProcessPath
}

function Ensure-Uv {
  Write-Step "Checking uv"
  Update-ProcessPath
  $uv = Get-Command uv -ErrorAction SilentlyContinue
  if (-not $uv) {
    Install-WithWinget -PackageId 'astral-sh.uv' -Name 'uv'
    $uv = Get-Command uv -ErrorAction SilentlyContinue
  }
  if (-not $uv) {
    throw "uv was installed, but setup cannot find it yet. Close this window, open setup.bat again, and it should continue."
  }
  Write-Host "Using uv: $($uv.Source)"
  return $uv.Source
}

function Ensure-Node {
  Write-Step "Checking Node.js and npm"
  $node = Get-Command node -ErrorAction SilentlyContinue
  $npm = Get-Command npm.cmd -ErrorAction SilentlyContinue
  if (-not $npm) {
    $npm = Get-Command npm -ErrorAction SilentlyContinue
  }

  $nodeOk = $false
  if ($node) {
    try {
      & $node.Source -e "const major = Number(process.versions.node.split('.')[0]); process.exit(major >= 18 ? 0 : 1)" *> $null
      $nodeOk = $LASTEXITCODE -eq 0
    } catch {
      $nodeOk = $false
    }
  }

  if ($nodeOk -and $npm) {
    Write-Host "Using Node.js: $($node.Source)"
    Write-Host "Using npm: $($npm.Source)"
    return $npm.Source
  }

  Install-WithWinget -PackageId 'OpenJS.NodeJS.LTS' -Name 'Node.js LTS'
  $npm = Get-Command npm.cmd -ErrorAction SilentlyContinue
  if (-not $npm) {
    $npm = Get-Command npm -ErrorAction SilentlyContinue
  }
  if (-not $npm) {
    throw "Node.js was installed, but setup cannot find npm yet. Close this window, open setup.bat again, and it should continue."
  }
  return $npm.Source
}

try {
  Write-Host "Monaw Agent one-click setup"
  Write-Host "Log file: $setupLog"
  "[$(Get-Date -Format o)] Monaw Agent setup started" | Set-Content -Path $setupLog

  Write-Step "Closing Monaw if it is running"
  Stop-MonawIfRunning

  $uv = Ensure-Uv
  $npm = Ensure-Node

  Write-Step "Syncing the locked Python backend environment"
  Invoke-LoggedCommand -FilePath $uv -ArgumentList @('sync', '--locked') -WorkingDirectory $backendDir

  Write-Step "Installing frontend packages"
  Invoke-LoggedCommand -FilePath $npm -ArgumentList @('ci') -WorkingDirectory $frontendDir

  Write-Step "Setup complete"
  Write-Host "You can now double-click start.bat to run Monaw Agent."
  "[$(Get-Date -Format o)] Monaw Agent setup completed" | Add-Content -Path $setupLog
  exit 0
} catch {
  Write-Host ""
  Write-Host "[ERROR] $($_.Exception.Message)"
  Write-Host "Setup log: $setupLog"
  "[$(Get-Date -Format o)] ERROR: $($_.Exception.Message)" | Add-Content -Path $setupLog
  exit 1
}
