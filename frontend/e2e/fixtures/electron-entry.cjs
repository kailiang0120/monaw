// Test-only entry: isolate Electron's legacy credential discovery before main.js loads.
const { app } = require('electron')
const fs = require('node:fs')
const path = require('node:path')
const os = require('node:os')

const root = path.resolve(process.env.MONAW_E2E_ROOT || '')
if (!root.startsWith(path.resolve(os.tmpdir()) + path.sep) || !path.basename(root).startsWith('monaw-e2e-')) {
  throw new Error('Application tests require a disposable monaw-e2e directory')
}
const appData = path.join(root, 'appdata')
fs.mkdirSync(appData, { recursive: true })
app.setPath('appData', appData)
app.on('browser-window-created', (_event, window) => window.hide())
require('../../electron/main.js')
