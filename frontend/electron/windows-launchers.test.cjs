const assert = require('node:assert/strict')
const { spawnSync } = require('node:child_process')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')
const test = require('node:test')

const repository = path.resolve(__dirname, '../..')
const windowsOnly = { skip: process.platform !== 'win32' }

/** Exercise launcher functions without running setup or touching the user's profile. */
function isolatedCheck(code, prepare = () => {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'monaw-launcher-test-'))
  try {
    prepare(root)
    return spawnSync('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command', code], {
      cwd: repository,
      encoding: 'utf8',
      timeout: 25_000,
      windowsHide: true,
      env: {
        ...process.env,
        MONAW_LAUNCHER_TEST_ROOT: root,
        MONAW_LAUNCHER_TEST_REPO: repository,
        MONAW_BACKEND_PORT: '',
        AGENT_UV_PATH: '',
        MONAW_HOME: root,
        AGENT_RUNTIME_DIR: path.join(root, 'runtime'),
      },
    })
  } finally {
    fs.rmSync(root, { recursive: true, force: true })
  }
}

test('Windows launchers resolve custom uv and validate the effective backend port', windowsOnly, () => {
  const result = isolatedCheck(`
    $ErrorActionPreference = 'Stop'
    Remove-Item Env:MONAW_BACKEND_PORT -ErrorAction SilentlyContinue
    . ./scripts/runtime-prerequisites.ps1
    $root = $env:MONAW_LAUNCHER_TEST_ROOT
    $config = Get-MonawBackendConfig -RepositoryRoot $root
    if ($config.Port -ne 8435) { throw 'Checked-in port was ignored' }
    $env:MONAW_BACKEND_PORT = '48001'
    if ((Get-MonawBackendConfig -RepositoryRoot $root).Port -ne 48001) { throw 'Port override was ignored' }
    foreach ($invalid in @('80', '65536', '-1', '8435junk', '99999999999999999999')) {
      $env:MONAW_BACKEND_PORT = $invalid
      $rejected = $false
      try { Get-MonawBackendConfig -RepositoryRoot $root | Out-Null } catch { $rejected = $true }
      if (-not $rejected) { throw "Accepted invalid port: $invalid" }
    }
    $env:AGENT_UV_PATH = Join-Path $root 'custom uv.exe'
    if ((Get-MonawUvCommand) -ne (Get-Item -LiteralPath $env:AGENT_UV_PATH).FullName) { throw 'Custom uv was ignored' }
    $env:AGENT_UV_PATH = Join-Path $root 'missing.exe'
    $rejected = $false
    try { Get-MonawUvCommand | Out-Null } catch { $rejected = $true }
    if (-not $rejected) { throw 'Missing explicit uv override was accepted' }
  `, (root) => {
    fs.mkdirSync(path.join(root, 'frontend', 'config'), { recursive: true })
    fs.writeFileSync(path.join(root, 'frontend', 'config', 'runtime.json'), JSON.stringify({ backendHost: '127.0.0.1', backendPort: 8435 }))
    fs.writeFileSync(path.join(root, 'custom uv.exe'), '')
  })
  assert.equal(result.status, 0, result.stdout + result.stderr)
})

test('setup streams native progress before completion and preserves command failures', windowsOnly, () => {
  const result = isolatedCheck(`
    $ErrorActionPreference = 'Stop'
    $tokens = $null
    $errors = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseFile((Join-Path $env:MONAW_LAUNCHER_TEST_REPO 'scripts/setup-windows.ps1'), [ref]$tokens, [ref]$errors)
    $function = $ast.Find({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Invoke-LoggedCommand' }, $true)
    Invoke-Expression $function.Extent.Text
    $repoRoot = $env:MONAW_LAUNCHER_TEST_ROOT
    $setupLog = Join-Path $repoRoot 'setup.log'
    $env:MONAW_LAUNCHER_TEST_LOG = $setupLog
    $shell = (Get-Process -Id $PID).Path
    $arguments = @('-NoProfile', '-NonInteractive', '-File', (Join-Path $repoRoot 'progress.ps1'))
    Invoke-LoggedCommand -FilePath $shell -ArgumentList $arguments
    if ((Get-Content -Raw $setupLog) -notmatch 'native-progress') { throw 'Native stderr was not logged' }
    $env:MONAW_LAUNCHER_TEST_FAIL = '1'
    $rejected = $false
    try { Invoke-LoggedCommand -FilePath $shell -ArgumentList $arguments } catch {
      $rejected = $_.Exception.Message -match 'exit code 7'
    }
    if (-not $rejected) { throw 'Native command failure did not propagate' }
  `, (root) => {
    fs.writeFileSync(path.join(root, 'progress.ps1'), `
      Write-Output 'stream-marker'
      $deadline = (Get-Date).AddSeconds(5)
      do {
        Start-Sleep -Milliseconds 100
        if ((Get-Content -Raw $env:MONAW_LAUNCHER_TEST_LOG) -match 'stream-marker') { break }
      } while ((Get-Date) -lt $deadline)
      if ((Get-Content -Raw $env:MONAW_LAUNCHER_TEST_LOG) -notmatch 'stream-marker') { exit 9 }
      [Console]::Error.WriteLine('native-progress')
      if ($env:MONAW_LAUNCHER_TEST_FAIL -eq '1') { exit 7 }
    `)
  })
  assert.equal(result.status, 0, result.stdout + result.stderr)
})

test('start fails before spawning services when source dependencies are missing', windowsOnly, () => {
  const result = isolatedCheck(`
    $env:AGENT_UV_PATH = Join-Path $env:MONAW_LAUNCHER_TEST_ROOT 'uv.cmd'
    & (Join-Path $env:MONAW_LAUNCHER_TEST_ROOT 'scripts/start-windows.ps1')
  `, (root) => {
    fs.writeFileSync(path.join(root, 'uv.cmd'), '@echo off\r\necho uv-test\r\nexit /b 0\r\n')
    for (const relative of [
      'scripts/start-windows.ps1',
      'scripts/node-prerequisites.ps1',
      'scripts/runtime-prerequisites.ps1',
      'frontend/package.json',
      'frontend/electron/node-support.cjs',
    ]) {
      const destination = path.join(root, relative)
      fs.mkdirSync(path.dirname(destination), { recursive: true })
      fs.copyFileSync(path.join(repository, relative), destination)
    }
  })
  assert.equal(result.status, 1, result.stdout + result.stderr)
  assert.match(result.stdout, /Missing backend\/\.venv\/Scripts\/python\.exe/)
  assert.doesNotMatch(result.stdout, /Starting Vite|Starting Electron/)
})
