const fs = require('fs')
const path = require('path')

const FROZEN_BACKEND = { win32: 'monaw-backend.exe' }

/**
 * How to start the backend. A packaged app runs the frozen backend shipped in
 * its resources, so the user needs no Python, uv, Node or Git. Development (and a
 * package built without the frozen backend) runs the source with uv.
 */
function backendLaunch({
  packaged,
  resourcesPath,
  sourceBackendDir,
  uvCommand,
  host,
  port,
  platform = process.platform,
  exists = fs.existsSync,
}) {
  const serve = ['--host', host, '--port', String(port)]
  const packagedBackendDir = packaged ? path.join(resourcesPath, 'backend') : ''
  if (packaged) {
    const executable = path.join(packagedBackendDir, FROZEN_BACKEND[platform] || 'monaw-backend')
    if (exists(executable)) {
      return {
        frozen: true,
        command: executable,
        args: serve,
        cwd: packagedBackendDir,
        // The extension ships beside the backend in resources, not beside the source tree.
        env: { MONAW_BROWSER_EXTENSION_SOURCE: path.join(resourcesPath, 'browser-extension') },
      }
    }
  }
  return {
    frozen: false,
    command: uvCommand || 'uv',
    args: ['run', '--locked', '--no-dev', 'uvicorn', 'app.main:app', ...serve],
    cwd: packaged ? packagedBackendDir : sourceBackendDir,
    env: {},
  }
}

module.exports = { backendLaunch }
