const assert = require('node:assert/strict')
const { execFileSync } = require('node:child_process')
const path = require('node:path')
const test = require('node:test')
const { isSupportedNodeVersion } = require('./node-support.cjs')
const manifest = require('../package.json')
const lock = require('../package-lock.json')

test('setup policy rejects unsupported release lines and accepts documented minimums', () => {
  for (const version of ['18.20.8', '20.18.9', '21.9.0', '22.11.9', '22.12.0', '22.12.9', '23.12.0', '25.0.0', '26.0.0', '22.12.0-rc.1', 'invalid']) {
    assert.equal(isSupportedNodeVersion(version), false, version)
  }
  for (const version of ['20.19.0', '20.20.1', 'v22.13.0', '22.18.0', '24.0.0', '24.9.1']) {
    assert.equal(isSupportedNodeVersion(version), true, version)
  }
  assert.equal(lock.packages[''].engines.node, manifest.engines.node)
})

test('standalone prerequisite probe uses the actual executable and package policy', () => {
  const probe = JSON.parse(execFileSync(process.execPath, [path.join(__dirname, 'node-support.cjs')], { encoding: 'utf8' }))
  assert.equal(probe.version, process.versions.node)
  assert.equal(probe.supported, true)
  assert.equal(probe.required, manifest.engines.node)
})

test('PowerShell prerequisite helper validates the resolved Node and npm', { skip: process.platform !== 'win32' }, () => {
  const root = path.resolve(__dirname, '../..')
  // Pass the path as a separate argument, without interpolating it into shell code.
  const command = '. ./scripts/node-prerequisites.ps1; $runtime = Get-MonawNodeRuntime; if (-not $runtime.supported) { throw "Node probe failed" }; $null = Assert-MonawNodeRuntime; $runtime | ConvertTo-Json -Compress'
  const output = execFileSync('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command', command], { cwd: root, encoding: 'utf8' })
  const runtime = JSON.parse(output.trim().split(/\r?\n/).at(-1))
  assert.equal(runtime.version, process.versions.node)
  assert.equal(runtime.required, manifest.engines.node)
  assert.ok(runtime.path)
})
