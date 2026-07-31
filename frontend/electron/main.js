const { app, BrowserWindow, ipcMain, dialog, session, shell, nativeTheme, safeStorage } = require('electron')
const path = require('path')
const os = require('os')
const { spawn, spawnSync } = require('child_process')
const http = require('http')
const fs = require('fs')
const { fileURLToPath } = require('url')
const runtimeConfig = require('../config/runtime.json')
const {
  generateControlSecret,
  mintControlSession,
  normalizeBackendHost,
} = require('./control-auth')
const { SECRET_FIELDS, createCredentialVault } = require('./credential-vault')
const CREDENTIAL_IDS = new Set(Object.keys(SECRET_FIELDS))
const MAX_IPC_STRING_LENGTH = 20_000

const BACKEND_HOST = normalizeBackendHost(
  runtimeConfig.backendHost,
  process.env.MONAW_ALLOW_UNSAFE_BACKEND_HOST === '1'
)
const BACKEND_PORT = runtimeConfig.backendPort || 8420
const BACKEND_BASE_URL = `http://${BACKEND_HOST}:${BACKEND_PORT}`
const CONTROL_SECRET = generateControlSecret()
const BACKEND_RESTART_EXIT_CODE = 78
const isDev = !app.isPackaged
const APP_USER_MODEL_ID = 'com.monaw.agent'
const LEGACY_USER_DATA_DIR_NAMES = ['AI Agent', 'ai-agent']
const APP_THEME_COLORS = {
  dark: '#111b13',
  light: '#f7fbdf',
}

let mainWindow = null
let backendProcess = null
let backendUvCommand = null
let appQuitting = false

function getBackendPath() {
  if (isDev) {
    return path.join(__dirname, '../../backend')
  }
  return path.join(process.resourcesPath, 'backend')
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

function attachBackendLogging(runtimeDir, proc) {
  const logPath = path.join(runtimeDir, 'backend.log')
  rotateFileIfNeeded(logPath)
  const logStream = fs.createWriteStream(logPath, { flags: 'a' })
  const writeLog = (streamName, data) => {
    const text = data.toString()
    logStream.write(`[${new Date().toISOString()}] [${streamName}] ${text}`)
  }

  console.log('[main] Backend log file:', logPath)
  proc.stdout?.on('data', (d) => writeLog('stdout', d))
  proc.stderr?.on('data', (d) => writeLog('stderr', d))
  proc.on('error', (err) => {
    logStream.write(`[${new Date().toISOString()}] [error] ${err.stack || err.message}\n`)
    console.error('[backend] process error', err.message)
  })
  proc.on('exit', (code) => {
    const line = `[${new Date().toISOString()}] [exit] code=${code}\n`
    logStream.write(line)
    logStream.end()
    console.log('[backend] exited with code', code)
    if (backendProcess === proc) backendProcess = null
    if (!appQuitting && code === BACKEND_RESTART_EXIT_CODE) {
      console.log('[main] Backend requested restart; respawning')
      setTimeout(() => {
        spawnBackend()
        waitForBackend().then(applyStoredCredentialsToBackend).catch((err) => {
          console.error('[main] Backend restart did not become ready:', err.message)
        })
      }, 500)
    }
  })
}

function spawnBackend() {
  const runtimeDir = getBackendRuntimeDir()
  const workspaceDir = getBackendWorkspaceDir()
  fs.mkdirSync(runtimeDir, { recursive: true })
  fs.mkdirSync(workspaceDir, { recursive: true })

  const backendDir = getBackendPath()
  const cmd = backendUvCommand || 'uv'

  console.log('[main] Spawning backend from', backendDir, 'with', cmd)

  const proc = spawn(
    cmd,
    ['run', '--locked', '--no-dev', 'uvicorn', 'app.main:app', '--host', BACKEND_HOST, '--port', String(BACKEND_PORT)],
    {
      cwd: backendDir,
      env: {
        ...process.env,
        MONAW_CONTROL_SECRET: CONTROL_SECRET,
        MONAW_ALLOW_DEVELOPMENT_TOKEN: '0',
        CORS_ALLOW_ORIGINS: isDev
          ? 'http://localhost:5275,http://127.0.0.1:5275'
          : 'null',
        CORS_ALLOW_ORIGIN_REGEX: '',
        AGENT_RUNTIME_DIR: process.env.AGENT_RUNTIME_DIR || runtimeDir,
        AGENT_WORKSPACE_DIR: process.env.AGENT_WORKSPACE_DIR || workspaceDir,
      },
      stdio: ['ignore', 'pipe', 'pipe'],
      windowsHide: true,
    }
  )
  backendProcess = proc
  attachBackendLogging(runtimeDir, proc)
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

function resolveUvCandidates() {
  const candidates = []
  if (process.env.AGENT_UV_PATH) candidates.push(process.env.AGENT_UV_PATH)
  candidates.push(process.platform === 'win32' ? 'uv.exe' : 'uv')
  return [...new Set(candidates)]
}

async function pickBackendUv() {
  for (const candidate of resolveUvCandidates()) {
    const result = await runCommandAndCapture(candidate, ['--version'])
    if (result.ok) return candidate
  }
  return process.platform === 'win32' ? 'uv.exe' : 'uv'
}

function killBackend() {
  if (!backendProcess) return
  const proc = backendProcess
  backendProcess = null
  if (process.platform === 'win32') {
    const result = spawnSync(
      'taskkill',
      ['/pid', String(proc.pid), '/t', '/f'],
      {
        windowsHide: true,
        stdio: 'ignore',
      },
    )
    if (result.error || result.status !== 0) {
      try {
        proc.kill()
      } catch {
        // The backend may have exited between the check and taskkill.
      }
    }
  } else {
    proc.kill('SIGTERM')
  }
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
      "object-src 'none'",
      "base-uri 'none'",
      "frame-ancestors 'none'",
      "form-action 'none'",
    ].join('; ')
  }

  return [
    "default-src 'self'",
    "script-src 'self'",
    `connect-src 'self' ${backendHttp}`,
    `img-src 'self' data: blob: ${backendHttp}`,
    "style-src 'self' 'unsafe-inline'",
    "font-src 'self'",
    "object-src 'none'",
    "base-uri 'none'",
    "frame-ancestors 'none'",
    "form-action 'none'",
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

function updateBackendSettings(payload) {
  return new Promise((resolve, reject) => {
    const body = Buffer.from(JSON.stringify(payload), 'utf8')
    const session = mintControlSession(CONTROL_SECRET)
    const request = http.request(
      `${BACKEND_BASE_URL}/api/settings`,
      {
        method: 'PUT',
        headers: {
          Authorization: `Bearer ${session.token}`,
          'Content-Type': 'application/json',
          'Content-Length': String(body.length),
        },
      },
      (response) => {
        const chunks = []
        response.on('data', (chunk) => chunks.push(chunk))
        response.on('end', () => {
          if (response.statusCode && response.statusCode >= 200 && response.statusCode < 300) {
            resolve()
            return
          }
          const detail = Buffer.concat(chunks).toString('utf8').slice(0, 500)
          reject(new Error(`Backend credential sync failed (${response.statusCode}): ${detail}`))
        })
      },
    )
    request.setTimeout(10_000, () => request.destroy(new Error('Backend credential sync timed out')))
    request.on('error', reject)
    request.on('finish', () => body.fill(0))
    request.end(body)
  })
}

function assertTrustedIpcSender(event) {
  if (!event?.sender || !isTrustedRenderer(event.sender, event.senderFrame?.url)) {
    throw new Error('IPC request rejected from untrusted renderer')
  }
}

function assertIpcString(value, name, maxLength = MAX_IPC_STRING_LENGTH) {
  if (typeof value !== 'string' || value.length > maxLength) {
    throw new Error(`IPC request rejected: invalid ${name}`)
  }
  return value
}

function assertOptionalIpcString(value, name, maxLength = MAX_IPC_STRING_LENGTH) {
  if (value === undefined || value === null || value === '') return ''
  return assertIpcString(value, name, maxLength)
}

function assertCredentialId(id) {
  const value = assertIpcString(id, 'credential id', 80)
  if (!CREDENTIAL_IDS.has(value)) {
    throw new Error('IPC request rejected: unsupported credential id')
  }
  return value
}

function assertTheme(value) {
  if (value !== 'light' && value !== 'dark') {
    throw new Error('IPC request rejected: invalid theme')
  }
  return value
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

function handleUntrustedNavigation(event, url) {
  if (isTrustedRendererUrl(url)) return
  if (openExternalUrl(url)) {
    event.preventDefault()
    return
  }
  event.preventDefault()
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
      sandbox: true,
      devTools: isDev && process.env.MONAW_DEVTOOLS === '1',
      additionalArguments: [`--monaw-backend-base-url=${BACKEND_BASE_URL}`],
    },
  })

  if (isDev) {
    mainWindow.loadURL('http://localhost:5275')
    if (process.env.MONAW_DEVTOOLS === '1') {
      mainWindow.webContents.openDevTools({ mode: 'detach' })
    }
  } else {
    mainWindow.loadFile(path.join(__dirname, '../dist/index.html'))
  }

  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (isTrustedRendererUrl(url)) return { action: 'allow' }
    if (openExternalUrl(url)) return { action: 'deny' }
    return { action: 'deny' }
  })

  mainWindow.webContents.on('will-navigate', handleUntrustedNavigation)
  mainWindow.webContents.on('will-frame-navigate', handleUntrustedNavigation)
  mainWindow.webContents.on('will-redirect', handleUntrustedNavigation)

  mainWindow.on('closed', () => { mainWindow = null })
}

app.whenReady().then(async () => {
  installContentSecurityPolicy()
  installMediaPermissionHandler()
  backendUvCommand = await pickBackendUv()
  spawnBackend()
  createWindow()

  waitForBackend().then(applyStoredCredentialsToBackend).catch((err) => {
    console.error('[main] Backend startup or credential sync failed:', err.message)
  })

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow()
  })
})

app.on('window-all-closed', () => {
  appQuitting = true
  killBackend()
  if (process.platform !== 'darwin') {
    app.quit()
  }
})

app.on('before-quit', () => {
  appQuitting = true
  killBackend()
})

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

async function getStore() {
  if (!store) {
    const { default: Store } = await import('electron-store')
    store = new Store()
  }
  return store
}

let credentialVault = null
async function getCredentialVault() {
  if (!credentialVault) {
    const currentStore = await getStore()
    const candidate = createCredentialVault({
      safeStorage,
      store: currentStore,
      legacyData: readLegacyStoreData() || {},
      deleteLegacyValue: deleteLegacyStoreKey,
    })
    candidate.migrate()
    credentialVault = candidate
  }
  return credentialVault
}

async function applyStoredCredentialsToBackend() {
  const vault = await getCredentialVault()
  const payload = vault.backendPayload()
  try {
    if (Object.keys(payload).length > 0) {
      await updateBackendSettings(payload)
    }
    return vault.status()
  } finally {
    for (const key of Object.keys(payload)) {
      payload[key] = ''
    }
  }
}

ipcMain.handle('control:get-session', async (event) => {
  assertTrustedIpcSender(event)
  return mintControlSession(CONTROL_SECRET)
})

ipcMain.handle('credentials:status', async (event) => {
  assertTrustedIpcSender(event)
  return (await getCredentialVault()).status()
})

ipcMain.handle('credentials:set', async (event, id, value) => {
  assertTrustedIpcSender(event)
  const credentialId = assertCredentialId(id)
  const credentialValue = assertIpcString(value, 'credential value')
  const vault = await getCredentialVault()
  vault.setCredential(credentialId, credentialValue)
  await updateBackendSettings({ [SECRET_FIELDS[credentialId].backendField]: credentialValue })
  return vault.status()
})

ipcMain.handle('credentials:delete', async (event, id) => {
  assertTrustedIpcSender(event)
  const credentialId = assertCredentialId(id)
  const vault = await getCredentialVault()
  vault.deleteCredential(credentialId)
  await updateBackendSettings({ [SECRET_FIELDS[credentialId].backendField]: '' })
  return vault.status()
})

ipcMain.handle('credentials:apply', async (event) => {
  assertTrustedIpcSender(event)
  return applyStoredCredentialsToBackend()
})

ipcMain.handle('theme:set', async (event, theme) => {
  assertTrustedIpcSender(event)
  applyNativeTheme(assertTheme(theme))
})

ipcMain.handle('dialog:select-directory', async (event, defaultPath) => {
  assertTrustedIpcSender(event)
  const safeDefaultPath = assertOptionalIpcString(defaultPath, 'default path', 4096)
  const result = await dialog.showOpenDialog(mainWindow, {
    title: 'Choose output folder',
    defaultPath: safeDefaultPath || undefined,
    properties: ['openDirectory', 'createDirectory'],
  })
  if (result.canceled || !result.filePaths.length) return ''
  return result.filePaths[0]
})
