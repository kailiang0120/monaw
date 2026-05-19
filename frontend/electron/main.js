const { app, BrowserWindow, ipcMain, dialog, session, shell, nativeTheme } = require('electron')
const path = require('path')
const os = require('os')
const { spawn } = require('child_process')
const http = require('http')
const fs = require('fs')
const { fileURLToPath } = require('url')
const runtimeConfig = require('../config/runtime.json')

const BACKEND_HOST = runtimeConfig.backendHost || '127.0.0.1'
const BACKEND_PORT = runtimeConfig.backendPort || 8420
const BACKEND_BASE_URL = `http://${BACKEND_HOST}:${BACKEND_PORT}`
const isDev = !app.isPackaged
const APP_USER_MODEL_ID = 'com.monaw.agent'
const STORE_KEYS = Array.isArray(runtimeConfig.allowedStoreKeys) ? runtimeConfig.allowedStoreKeys : []
const ALLOWED_STORE_KEYS = new Set(STORE_KEYS)
const LEGACY_USER_DATA_DIR_NAMES = ['AI Agent', 'ai-agent']
const APP_THEME_COLORS = {
  dark: '#111b13',
  light: '#f7fbdf',
}

let mainWindow = null
let backendProcess = null
let backendPythonCommand = null

function getBackendPath() {
  if (isDev) {
    return path.join(__dirname, '../../backend')
  }
  return path.join(process.resourcesPath, 'backend')
}

function assertAllowedStoreKey(key) {
  if (typeof key !== 'string' || !ALLOWED_STORE_KEYS.has(key)) {
    throw new Error(`Unsupported store key: ${String(key)}`)
  }
  return key
}

function assertStoreValue(value) {
  if (typeof value !== 'string') {
    throw new Error('Stored values must be strings')
  }
  return value
}

function getMonawHomeDir() {
  return process.env.MONAW_HOME || process.env.AGENT_HOME || path.join(os.homedir(), '.monaw')
}

function installUserDataDir() {
  const userDataDir = path.join(getMonawHomeDir(), 'runtime', 'electron')
  fs.mkdirSync(userDataDir, { recursive: true })
  app.setPath('userData', userDataDir)
}

function getBackendRuntimeDir() {
  return path.join(getMonawHomeDir(), 'runtime')
}

function getBackendWorkspaceDir() {
  return process.env.AGENT_WORKSPACE_DIR || path.join(getMonawHomeDir(), 'workspace')
}

function rotateFileIfNeeded(filePath, maxBytes = 5 * 1024 * 1024) {
  try {
    if (!fs.existsSync(filePath) || fs.statSync(filePath).size <= maxBytes) return
    const rotatedPath = `${filePath}.1`
    if (fs.existsSync(rotatedPath)) fs.unlinkSync(rotatedPath)
    fs.renameSync(filePath, rotatedPath)
  } catch (err) {
    console.warn('[main] Failed to rotate log', filePath, err.message)
  }
}

function attachBackendLogging(runtimeDir) {
  const logPath = path.join(runtimeDir, 'backend.log')
  rotateFileIfNeeded(logPath)
  const logStream = fs.createWriteStream(logPath, { flags: 'a' })
  const writeLog = (streamName, data) => {
    const text = data.toString()
    logStream.write(`[${new Date().toISOString()}] [${streamName}] ${text}`)
  }

  console.log('[main] Backend log file:', logPath)
  backendProcess.stdout?.on('data', (d) => writeLog('stdout', d))
  backendProcess.stderr?.on('data', (d) => writeLog('stderr', d))
  backendProcess.on('error', (err) => {
    logStream.write(`[${new Date().toISOString()}] [error] ${err.stack || err.message}\n`)
    console.error('[backend] process error', err.message)
  })
  backendProcess.on('exit', (code) => {
    const line = `[${new Date().toISOString()}] [exit] code=${code}\n`
    logStream.write(line)
    logStream.end()
    console.log('[backend] exited with code', code)
  })
}

function spawnBackend() {
  const runtimeDir = getBackendRuntimeDir()
  const workspaceDir = getBackendWorkspaceDir()
  fs.mkdirSync(runtimeDir, { recursive: true })
  fs.mkdirSync(workspaceDir, { recursive: true })

  const backendDir = getBackendPath()
  const cmd = backendPythonCommand || (process.platform === 'win32' ? 'python' : 'python3')

  console.log('[main] Spawning Python backend from', backendDir, 'with', cmd)

  backendProcess = spawn(
    cmd,
    ['-m', 'uvicorn', 'app.main:app', '--host', BACKEND_HOST, '--port', String(BACKEND_PORT)],
    {
      cwd: backendDir,
      env: {
        ...process.env,
        BACKEND_PYTHON_COMMAND: cmd,
        AGENT_RUNTIME_DIR: process.env.AGENT_RUNTIME_DIR || runtimeDir,
        AGENT_WORKSPACE_DIR: process.env.AGENT_WORKSPACE_DIR || workspaceDir,
      },
      stdio: ['ignore', 'pipe', 'pipe'],
      windowsHide: true,
    }
  )
  attachBackendLogging(runtimeDir)
}

installUserDataDir()
if (process.platform === 'win32') {
  app.setAppUserModelId(APP_USER_MODEL_ID)
}

function runCommandAndCapture(cmd, args, options = {}) {
  return new Promise((resolve) => {
    const proc = spawn(cmd, args, options)
    let stdout = ''
    let stderr = ''
    proc.stdout?.on('data', (d) => { stdout += d.toString() })
    proc.stderr?.on('data', (d) => { stderr += d.toString() })
    proc.on('error', (err) => {
      resolve({ ok: false, code: -1, stdout, stderr: String(err) })
    })
    proc.on('close', (code) => {
      resolve({ ok: code === 0, code, stdout, stderr })
    })
  })
}

function resolvePythonCandidates() {
  const candidates = []
  if (process.env.AGENT_PYTHON_PATH) candidates.push(process.env.AGENT_PYTHON_PATH)
  if (process.env.PYTHON_PATH) candidates.push(process.env.PYTHON_PATH)

  const backendDir = getBackendPath()
  const venvWin = path.join(backendDir, '.venv', 'Scripts', 'python.exe')
  const venvUnix = path.join(backendDir, '.venv', 'bin', 'python')
  if (fs.existsSync(venvWin)) candidates.push(venvWin)
  if (fs.existsSync(venvUnix)) candidates.push(venvUnix)

  if (process.platform === 'win32') {
    candidates.push('python')
    candidates.push('py')
  } else {
    candidates.push('python3')
    candidates.push('python')
  }
  return [...new Set(candidates)]
}

async function pickBackendPython() {
  for (const candidate of resolvePythonCandidates()) {
    const args = candidate === 'py' ? ['-3', '--version'] : ['--version']
    const result = await runCommandAndCapture(candidate, args)
    if (result.ok) return candidate
  }
  return process.platform === 'win32' ? 'python' : 'python3'
}

function killBackend() {
  if (!backendProcess) return
  if (process.platform === 'win32') {
    spawn('taskkill', ['/pid', String(backendProcess.pid), '/f', '/t'])
  } else {
    backendProcess.kill('SIGTERM')
  }
  backendProcess = null
}

function waitForBackend(retries = 30, delay = 1000) {
  return new Promise((resolve, reject) => {
    let attempts = 0
    const check = () => {
      http
        .get(`${BACKEND_BASE_URL}/health`, (res) => {
          if (res.statusCode === 200) {
            console.log('[main] Backend ready')
            resolve()
          } else {
            retry()
          }
        })
        .on('error', retry)
    }
    const retry = () => {
      attempts++
      if (attempts >= retries) {
        reject(new Error('Backend failed to start'))
      } else {
        setTimeout(check, delay)
      }
    }
    check()
  })
}

function buildContentSecurityPolicy() {
  const backendHttp = BACKEND_BASE_URL
  const viteHttp = 'http://localhost:5275'
  const viteLoopbackHttp = 'http://127.0.0.1:5275'
  const viteWs = 'ws://localhost:5275'
  const viteLoopbackWs = 'ws://127.0.0.1:5275'

  if (isDev) {
    return [
      "default-src 'self'",
      `script-src 'self' 'unsafe-inline' 'unsafe-eval' ${viteHttp} ${viteLoopbackHttp}`,
      `connect-src 'self' ${backendHttp} ${viteHttp} ${viteLoopbackHttp} ${viteWs} ${viteLoopbackWs}`,
      `img-src 'self' data: blob: ${backendHttp}`,
      "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
      "font-src 'self' https://fonts.gstatic.com",
    ].join('; ')
  }

  return [
    "default-src 'self'",
    "script-src 'self'",
    `connect-src 'self' ${backendHttp}`,
    `img-src 'self' data: blob: ${backendHttp}`,
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
    "font-src 'self' https://fonts.gstatic.com",
  ].join('; ')
}

function installContentSecurityPolicy() {
  const policy = buildContentSecurityPolicy()
  session.defaultSession.webRequest.onHeadersReceived((details, callback) => {
    callback({
      responseHeaders: {
        ...details.responseHeaders,
        'Content-Security-Policy': [policy],
      },
    })
  })
}

function isTrustedRendererUrl(value) {
  if (!value) return false
  try {
    const url = new URL(value)
    if (isDev) return ['localhost', '127.0.0.1'].includes(url.hostname) && url.port === '5275'
    if (url.protocol !== 'file:') return false
    return path.resolve(fileURLToPath(url)) === path.resolve(__dirname, '../dist/index.html')
  } catch {
    return false
  }
}

function isTrustedRenderer(webContents, value) {
  if (!mainWindow || webContents.id !== mainWindow.webContents.id) return false
  return isTrustedRendererUrl(value || webContents.getURL())
}

function isExternalOpenableUrl(value) {
  try {
    const url = new URL(value)
    return url.protocol === 'http:' || url.protocol === 'https:'
  } catch {
    return false
  }
}

function openExternalUrl(value) {
  if (!isExternalOpenableUrl(value)) return false
  shell.openExternal(value).catch((err) => {
    console.warn('[main] Failed to open external URL', value, err.message)
  })
  return true
}

function installMediaPermissionHandler() {
  session.defaultSession.setPermissionCheckHandler((webContents, permission, requestingOrigin) => {
    const allowed = permission === 'media' && isTrustedRenderer(webContents, requestingOrigin)
    console.log('[main] Permission check', { permission, requestingOrigin, allowed })
    return allowed
  })
  session.defaultSession.setPermissionRequestHandler((webContents, permission, callback, details) => {
    const requestingUrl = (details && details.requestingUrl) || webContents.getURL()
    const allowed = permission === 'media' && isTrustedRenderer(webContents, requestingUrl)
    console.log('[main] Permission request', { permission, requestingUrl, allowed })
    callback(allowed)
  })
}

function getAppIconPath() {
  const iconPath = path.join(__dirname, '../assets/app-icon.png')
  return fs.existsSync(iconPath) ? iconPath : undefined
}

function applyNativeTheme(theme) {
  const safeTheme = theme === 'light' ? 'light' : 'dark'
  nativeTheme.themeSource = safeTheme
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.setBackgroundColor(APP_THEME_COLORS[safeTheme])
  }
}

function createWindow() {
  const appIconPath = getAppIconPath()
  if (process.platform === 'darwin' && appIconPath && app.dock) {
    app.dock.setIcon(appIconPath)
  }

  mainWindow = new BrowserWindow({
    width: 1200,
    height: 800,
    minWidth: 800,
    minHeight: 600,
    titleBarStyle: process.platform === 'darwin' ? 'hiddenInset' : 'default',
    frame: process.platform !== 'darwin',
    autoHideMenuBar: true,
    backgroundColor: APP_THEME_COLORS.dark,
    ...(appIconPath ? { icon: appIconPath } : {}),
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
      devTools: false,
    },
  })

  if (isDev) {
    mainWindow.loadURL('http://localhost:5275')
    mainWindow.webContents.openDevTools()
  } else {
    mainWindow.loadFile(path.join(__dirname, '../dist/index.html'))
  }

  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (isTrustedRendererUrl(url)) return { action: 'allow' }
    if (openExternalUrl(url)) return { action: 'deny' }
    return { action: 'deny' }
  })

  mainWindow.webContents.on('will-navigate', (event, url) => {
    if (isTrustedRendererUrl(url)) return
    if (openExternalUrl(url)) {
      event.preventDefault()
      return
    }
    event.preventDefault()
  })

  mainWindow.on('closed', () => { mainWindow = null })
}

app.whenReady().then(async () => {
  installContentSecurityPolicy()
  installMediaPermissionHandler()
  if (isDev) {
    backendPythonCommand = await pickBackendPython()
  }
  spawnBackend()

  try {
    await waitForBackend()
  } catch (err) {
    console.error('[main] Backend did not start in time:', err.message)
  }

  createWindow()

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow()
  })
})

app.on('window-all-closed', () => {
  killBackend()
  if (process.platform !== 'darwin') app.quit()
})

app.on('before-quit', killBackend)

let store = null
function readLegacyStoreData() {
  const appDataDir = app.getPath('appData')
  const currentUserData = path.resolve(app.getPath('userData'))
  const merged = {}
  for (const dirName of LEGACY_USER_DATA_DIR_NAMES) {
    const configPath = path.join(appDataDir, dirName, 'config.json')
    const legacyUserData = path.resolve(path.dirname(configPath))
    if (legacyUserData === currentUserData || !fs.existsSync(configPath)) continue
    try {
      const parsed = JSON.parse(fs.readFileSync(configPath, 'utf8'))
      if (parsed && typeof parsed === 'object') Object.assign(merged, parsed)
    } catch (err) {
      console.warn('[main] Failed to read legacy key store', configPath, err.message)
    }
  }
  return Object.keys(merged).length > 0 ? merged : null
}

function legacyConfigPaths() {
  const appDataDir = app.getPath('appData')
  const currentUserData = path.resolve(app.getPath('userData'))
  return LEGACY_USER_DATA_DIR_NAMES
    .map((dirName) => path.join(appDataDir, dirName, 'config.json'))
    .filter((configPath) => path.resolve(path.dirname(configPath)) !== currentUserData)
}

function deleteLegacyStoreKey(key) {
  for (const configPath of legacyConfigPaths()) {
    if (!fs.existsSync(configPath)) continue
    try {
      const parsed = JSON.parse(fs.readFileSync(configPath, 'utf8'))
      if (!parsed || typeof parsed !== 'object' || !(key in parsed)) continue
      delete parsed[key]
      fs.writeFileSync(configPath, JSON.stringify(parsed, null, 2), 'utf8')
    } catch (err) {
      console.warn('[main] Failed to delete legacy key', configPath, key, err.message)
    }
  }
}

function migrateLegacySecrets(currentStore) {
  const legacyData = readLegacyStoreData()
  if (!legacyData) return
  let migrated = false
  for (const key of STORE_KEYS) {
    const currentValue = currentStore.get(key)
    const legacyValue = legacyData[key]
    if (typeof currentValue === 'undefined' && typeof legacyValue === 'string' && legacyValue) {
      currentStore.set(key, legacyValue)
      migrated = true
    }
  }
  if (migrated) {
    console.log('[main] Migrated saved API keys from legacy app data store')
  }
}

async function getStore() {
  if (!store) {
    const { default: Store } = await import('electron-store')
    store = new Store()
    migrateLegacySecrets(store)
  }
  return store
}

ipcMain.handle('store:get', async (_, key) => {
  const s = await getStore()
  const safeKey = assertAllowedStoreKey(key)
  return s.get(safeKey)
})

ipcMain.handle('store:set', async (_, key, value) => {
  const s = await getStore()
  const safeKey = assertAllowedStoreKey(key)
  const safeValue = assertStoreValue(value)
  if (safeValue === '') {
    s.delete(safeKey)
    deleteLegacyStoreKey(safeKey)
    return
  }
  s.set(safeKey, safeValue)
})

ipcMain.handle('store:delete', async (_, key) => {
  const s = await getStore()
  const safeKey = assertAllowedStoreKey(key)
  s.delete(safeKey)
  deleteLegacyStoreKey(safeKey)
})

ipcMain.handle('theme:set', async (_, theme) => {
  applyNativeTheme(theme)
})

ipcMain.handle('dialog:select-directory', async (_, defaultPath) => {
  const result = await dialog.showOpenDialog(mainWindow, {
    title: 'Choose output folder',
    defaultPath: defaultPath || undefined,
    properties: ['openDirectory', 'createDirectory'],
  })
  if (result.canceled || !result.filePaths.length) return ''
  return result.filePaths[0]
})
