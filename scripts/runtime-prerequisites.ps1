# Refresh newly installed tools without changing the caller's existing PATH priority.
function Update-MonawProcessPath {
  $machinePath = [Environment]::GetEnvironmentVariable('Path', 'Machine')
  $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
  $env:Path = @($env:Path, $machinePath, $userPath) -join ';'
}

# Resolve the same explicit uv override used by Electron, or the application on PATH.
function Get-MonawUvCommand {
  if ($env:AGENT_UV_PATH) {
    if (-not (Test-Path -LiteralPath $env:AGENT_UV_PATH -PathType Leaf)) {
      throw "AGENT_UV_PATH does not point to a file: '$env:AGENT_UV_PATH'. Set it to the full path of uv.exe, or remove the override."
    }
    return (Get-Item -LiteralPath $env:AGENT_UV_PATH).FullName
  }
  $uv = Get-Command uv.exe -CommandType Application -ErrorAction SilentlyContinue
  if ($uv) { return $uv.Source }
  return $null
}

# Read the checked-in host and the port override using Electron's validation rules.
function Get-MonawBackendConfig {
  param([string] $RepositoryRoot = (Split-Path -Parent $PSScriptRoot))

  $configPath = Join-Path $RepositoryRoot 'frontend/config/runtime.json'
  try {
    $config = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
    if ($null -eq $config -or $config -isnot [pscustomobject]) {
      throw 'Expected a JSON object with backendHost and backendPort.'
    }
  } catch {
    throw "Cannot read backend configuration at '$configPath': $($_.Exception.Message)"
  }
  $backendHost = if ($config.backendHost) {
    ([string]$config.backendHost).Trim().ToLowerInvariant()
  } else {
    '127.0.0.1'
  }
  $portValue = if ($null -ne $env:MONAW_BACKEND_PORT) {
    $env:MONAW_BACKEND_PORT
  } elseif ($null -ne $config.backendPort) {
    $config.backendPort
  } else {
    8420
  }
  $parsedPort = 0
  $validPort = [string]$portValue -match '^\d+$' -and
    [int]::TryParse([string]$portValue, [ref]$parsedPort) -and
    $parsedPort -ge 1024 -and $parsedPort -le 65535
  if (-not $validPort) {
    throw 'Invalid Monaw backend port. MONAW_BACKEND_PORT or frontend/config/runtime.json must specify an integer from 1024 to 65535.'
  }
  return [pscustomobject]@{
    HostName = $backendHost
    Port = $parsedPort
  }
}
