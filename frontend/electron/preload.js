const { contextBridge, ipcRenderer } = require('electron')
const runtimeConfig = require('../config/runtime.json')

const backendBaseUrl = `http://${runtimeConfig.backendHost || '127.0.0.1'}:${runtimeConfig.backendPort || 8420}`

contextBridge.exposeInMainWorld('electronAPI', {
  getControlSession: () => ipcRenderer.invoke('control:get-session'),
  credentialStatus: () => ipcRenderer.invoke('credentials:status'),
  setCredential: (id, value) => ipcRenderer.invoke('credentials:set', id, value),
  deleteCredential: (id) => ipcRenderer.invoke('credentials:delete', id),
  applyStoredCredentials: () => ipcRenderer.invoke('credentials:apply'),
  selectDirectory: (defaultPath) => ipcRenderer.invoke('dialog:select-directory', defaultPath),
  setTheme: (theme) => ipcRenderer.invoke('theme:set', theme),
  backendBaseUrl,
  isElectron: true,
})
