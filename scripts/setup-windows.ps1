$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
$backendDir = Join-Path $repoRoot 'backend'
$frontendDir = Join-Path $repoRoot 'frontend'
$venvDir = Join-Path $backendDir '.venv'
$venvPython = Join-Path $venvDir 'Scripts\python.exe'
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
  try {
    $output = & $FilePath @ArgumentList 2>&1
    $exitCode = if ($null -ne $LASTEXITCODE) { $LASTEXITCODE } else { 0 }
  } finally {
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

function Test-PythonCommand {
  param(
    [Parameter(Mandatory = $true)] [string] $Command,
    [string[]] $PrefixArgs = @()
  )

  try {
    & $Command @PrefixArgs -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" *> $null
    return $LASTEXITCODE -eq 0
  } catch {
    return $false
  }
}

function Resolve-Python {
  if (Test-Path $venvPython) {
    return [pscustomobject]@{ Command = $venvPython; Args = @(); Display = $venvPython }
  }
  if (Test-PythonCommand -Command 'py' -PrefixArgs @('-3.11')) {
    return [pscustomobject]@{ Command = 'py'; Args = @('-3.11'); Display = 'py -3.11' }
  }
  if (Test-PythonCommand -Command 'py' -PrefixArgs @('-3')) {
    return [pscustomobject]@{ Command = 'py'; Args = @('-3'); Display = 'py -3' }
  }
  if (Test-PythonCommand -Command 'python') {
    return [pscustomobject]@{ Command = 'python'; Args = @(); Display = 'python' }
  }
  return $null
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

function Ensure-Python {
  Write-Step "Checking Python"
  $python = Resolve-Python
  if ($python) {
    Write-Host "Using Python: $($python.Display)"
    return $python
  }

  Install-WithWinget -PackageId 'Python.Python.3.11' -Name 'Python 3.11'
  $python = Resolve-Python
  if (-not $python) {
    throw "Python was installed, but setup cannot find it yet. Close this window, open setup.bat again, and it should continue."
  }
  Write-Host "Using Python: $($python.Display)"
  return $python
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

  $python = Ensure-Python
  $npm = Ensure-Node

  Write-Step "Creating Python virtual environment"
  if (-not (Test-Path $venvPython)) {
    Invoke-LoggedCommand -FilePath $python.Command -ArgumentList @($python.Args + @('-m', 'venv', $venvDir)) -WorkingDirectory $backendDir
  } else {
    Write-Host "Virtual environment already exists: $venvDir"
  }

  Write-Step "Installing Python backend requirements"
  Invoke-LoggedCommand -FilePath $venvPython -ArgumentList @('-m', 'pip', 'install', '--upgrade', 'pip') -WorkingDirectory $backendDir
  Invoke-LoggedCommand -FilePath $venvPython -ArgumentList @('-m', 'pip', 'install', '-r', 'requirements.txt') -WorkingDirectory $backendDir

  Write-Step "Installing frontend packages"
  Invoke-LoggedCommand -FilePath $npm -ArgumentList @('install') -WorkingDirectory $frontendDir

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
