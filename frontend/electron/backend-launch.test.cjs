const assert = require('node:assert/strict')
const path = require('node:path')
const test = require('node:test')
const { backendLaunch } = require('./backend-launch.cjs')

const base = {
  resourcesPath: path.join('C:', 'Program Files', 'Monaw', 'resources'),
  sourceBackendDir: path.join('C:', 'src', 'monaw', 'backend'),
  uvCommand: 'uv.exe',
  host: '127.0.0.1',
  port: 8435,
  platform: 'win32',
}

test('a packaged app runs the frozen backend from its resources', () => {
  const launch = backendLaunch({ ...base, packaged: true, exists: () => true })
  const backendDir = path.join(base.resourcesPath, 'backend')

  assert.equal(launch.frozen, true)
  assert.equal(launch.command, path.join(backendDir, 'monaw-backend.exe'))
  assert.deepEqual(launch.args, ['--host', '127.0.0.1', '--port', '8435'])
  assert.equal(launch.cwd, backendDir)
  assert.equal(launch.env.MONAW_BROWSER_EXTENSION_SOURCE, path.join(base.resourcesPath, 'browser-extension'))
})

test('development runs the source backend with uv', () => {
  const launch = backendLaunch({ ...base, packaged: false, exists: () => true })

  assert.equal(launch.frozen, false)
  assert.equal(launch.command, 'uv.exe')
  assert.deepEqual(launch.args, ['run', '--locked', '--no-dev', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', '8435'])
  assert.equal(launch.cwd, base.sourceBackendDir)
  assert.deepEqual(launch.env, {})
})

test('a package without the frozen backend keeps the uv launch from its resources', () => {
  const launch = backendLaunch({ ...base, packaged: true, uvCommand: null, exists: () => false })

  assert.equal(launch.frozen, false)
  assert.equal(launch.command, 'uv')
  assert.equal(launch.cwd, path.join(base.resourcesPath, 'backend'))
})
