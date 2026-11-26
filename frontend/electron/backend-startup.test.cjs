const assert = require('node:assert/strict')
const net = require('node:net')
const test = require('node:test')
const { EventEmitter } = require('node:events')
const { portInUse, startupFailureMessage } = require('./backend-startup.cjs')
const { showStartupMessage } = require('./message-window.cjs')

test('detects a port another program is listening on, and a free one', async () => {
  const other = net.createServer()
  await new Promise((resolve) => other.listen(0, '127.0.0.1', resolve))
  const { port } = other.address()
  try {
    assert.equal(await portInUse('127.0.0.1', port), true)
  } finally {
    await new Promise((resolve) => other.close(resolve))
  }
  assert.equal(await portInUse('127.0.0.1', port), false)
})

test('startup failures explain what happened without stopping anything for the user', () => {
  const busy = startupFailureMessage({ kind: 'port_in_use' }, { port: 8435, logPath: 'C:\\logs\\backend.log' })
  assert.equal(busy.message, 'Port 8435 is already in use')
  assert.match(busy.detail, /Monaw did not stop that program/)

  const crashed = startupFailureMessage({ kind: 'exited', code: 3 }, { port: 8435, logPath: 'C:\\logs\\backend.log' })
  assert.match(crashed.detail, /exited with code 3/)
  assert.match(crashed.detail, /C:\\logs\\backend\.log/)
})

test('startup failure can display and quit without a main renderer or privileged preload', () => {
  let closed = false
  class FailureWindow extends EventEmitter {
    constructor(options) {
      super()
      this.options = options
      this.webContents = new EventEmitter()
      this.webContents.setWindowOpenHandler = (handler) => { this.openHandler = handler }
    }
    // Record the local startup document instead of requiring a running desktop.
    loadFile(file, options) {
      this.loaded = { file, options }
      return Promise.resolve()
    }
    // Model the window lifecycle used by the main process.
    show() { this.visible = true }
    close() { this.emit('closed') }
  }
  const popup = showStartupMessage({
    BrowserWindow: FailureWindow,
    theme: 'light',
    title: 'Monaw could not start',
    message: 'Port 8435 is already in use',
    detail: 'Monaw did not stop that program.',
    onClose: () => { closed = true },
  })
  assert.match(popup.loaded.file, /startup-message\.html$/)
  assert.equal(popup.loaded.options.query.detail, 'Monaw did not stop that program.')
  assert.deepEqual(popup.options.webPreferences, { contextIsolation: true, nodeIntegration: false, sandbox: true })
  assert.deepEqual(popup.openHandler(), { action: 'deny' })
  popup.emit('ready-to-show')
  assert.equal(popup.visible, true)
  popup.close()
  assert.equal(closed, true)
})
