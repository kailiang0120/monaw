const path = require('node:path')

/** Show a local themed failure even when the backend or main renderer cannot start. */
function showStartupMessage({ BrowserWindow, parent, theme, title, message, detail, onClose }) {
  const window = new BrowserWindow({
    width: 520,
    height: 380,
    minWidth: 360,
    minHeight: 260,
    show: false,
    frame: false,
    autoHideMenuBar: true,
    title,
    backgroundColor: theme === 'light' ? '#faf9f5' : '#262624',
    ...(parent && !parent.isDestroyed() ? { parent, modal: true } : {}),
    webPreferences: { contextIsolation: true, nodeIntegration: false, sandbox: true },
  })
  window.webContents.setWindowOpenHandler(() => ({ action: 'deny' }))
  window.webContents.on('will-navigate', (event) => event.preventDefault())
  window.once('ready-to-show', () => window.show())
  window.once('closed', onClose)
  window.loadFile(path.join(__dirname, 'startup-message.html'), {
    query: { theme: theme === 'light' ? 'light' : 'dark', title, message, detail },
  }).catch((error) => {
    console.error('[main] Could not display startup failure:', error.message)
    window.close()
  })
  return window
}

module.exports = { showStartupMessage }
