function Get-MonawNodeRuntime {
  param([string] $RepositoryRoot = (Split-Path -Parent $PSScriptRoot))

  $node = Get-Command node -ErrorAction SilentlyContinue
  $required = (Get-Content -LiteralPath (Join-Path $RepositoryRoot 'frontend/package.json') -Raw | ConvertFrom-Json).engines.node
  if (-not $node) {
    return [pscustomobject]@{ supported = $false; version = 'missing'; path = ''; required = $required }
  }
  $probe = & $node.Source (Join-Path $RepositoryRoot 'frontend/electron/node-support.cjs')
  if ($probe) {
    try {
      $result = $probe | ConvertFrom-Json
      return [pscustomobject]@{
        supported = ($LASTEXITCODE -eq 0 -and $result.supported)
        version = $result.version; path = $node.Source; required = $required
      }
    } catch {
      # An executable too old to run the probe is unsupported too.
    }
  }
  return [pscustomobject]@{ supported = $false; version = 'unrecognized'; path = $node.Source; required = $required }
}

function Assert-MonawNodeRuntime {
  $runtime = Get-MonawNodeRuntime
  if (-not $runtime.supported) {
    throw "Unsupported Node.js $($runtime.version) at '$($runtime.path)'. Required: $($runtime.required). Install a supported Node.js LTS, then reopen the terminal. If an older node.exe is earlier on PATH, remove or reorder that entry."
  }
  $npm = Get-Command npm.cmd -ErrorAction SilentlyContinue
  if (-not $npm) { $npm = Get-Command npm -ErrorAction SilentlyContinue }
  if (-not $npm) {
    throw "Node.js $($runtime.version) is supported, but npm is missing. Install Node.js with npm, then reopen the terminal."
  }
  Write-Host "Using Node.js $($runtime.version): $($runtime.path)"
  Write-Host "Using npm: $($npm.Source)"
  return $npm.Source
}
