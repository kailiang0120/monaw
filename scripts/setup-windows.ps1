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

function Assert-MonawStopped {
  $escapedRepoRoot = [regex]::Escape($repoRoot)
  $blockingProcesses = @(
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
      Where-Object {
        $_.Name -in @('electron.exe', 'node.exe', 'npm.exe', 'uv.exe', 'uvicorn.exe', 'python.exe') -and
        $_.CommandLine -and
        $_.CommandLine -match $escapedRepoRoot
      }
  )

  $blockingPorts = @(
    Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
      Where-Object { $_.LocalPort -in @(5275, 8420) }
  )

  if ($blockingProcesses.Count -eq 0 -and $blockingPorts.Count -eq 0) {
    return
  }

  $details = @()
  if ($blockingProcesses.Count -gt 0) {
    $processDetails = $blockingProcesses |
      ForEach-Object { "$($_.Name) (PID $($_.ProcessId))" }
    $details += "processes: $($processDetails -join ', ')"
  }
  if ($blockingPorts.Count -gt 0) {
    $portDetails = $blockingPorts |
      ForEach-Object { "$($_.LocalPort) (PID $($_.OwningProcess))" }
    $details += "ports: $($portDetails -join ', ')"
  }

  throw "Monaw appears to be running ($($details -join '; ')). Close Monaw and its launcher, then run setup.bat again."
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

  Write-Step "Checking for running Monaw processes"
  Assert-MonawStopped

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
