const { contextBridge, ipcRenderer } = require('electron')

const backendBaseUrlPrefix = '--monaw-backend-base-url='
const backendBaseUrlArgument = process.argv.find((value) => value.startsWith(backendBaseUrlPrefix))
const backendBaseUrlCandidate = backendBaseUrlArgument
  ? backendBaseUrlArgument.slice(backendBaseUrlPrefix.length)
  : ''
const backendBaseUrl = /^http:\/\/(?:127\.0\.0\.1|localhost|\[::1\]):\d+$/.test(backendBaseUrlCandidate)
  ? backendBaseUrlCandidate
  : ''
const credentialIds = new Set(['openai', 'google', 'tavily', 'telegramBot'])

function credentialId(value) {
  if (typeof value !== 'string' || !credentialIds.has(value)) {
    throw new TypeError('Unsupported credential id')
  }
  return value
}

function safeString(value, name, maxLength = 20000) {
  if (typeof value !== 'string' || value.length > maxLength) {
    throw new TypeError(`Invalid ${name}`)
  }
  return value
}

function optionalString(value, name, maxLength = 4096) {
  if (value === undefined || value === null || value === '') return ''
  return safeString(value, name, maxLength)
}

function theme(value) {
  if (value !== 'dark' && value !== 'light') {
    throw new TypeError('Invalid theme')
  }
  return value
}

const electronAPI = Object.freeze({
  getControlSession: () => ipcRenderer.invoke('control:get-session'),
  credentialStatus: () => ipcRenderer.invoke('credentials:status'),
  setCredential: (id, value) => ipcRenderer.invoke('credentials:set', credentialId(id), safeString(value, 'credential value')),
  deleteCredential: (id) => ipcRenderer.invoke('credentials:delete', credentialId(id)),
  applyStoredCredentials: () => ipcRenderer.invoke('credentials:apply'),
  selectDirectory: (defaultPath) => ipcRenderer.invoke('dialog:select-directory', optionalString(defaultPath, 'default path')),
  setTheme: (value) => ipcRenderer.invoke('theme:set', theme(value)),
  backendBaseUrl,
  isElectron: true,
})

contextBridge.exposeInMainWorld('electronAPI', electronAPI)
