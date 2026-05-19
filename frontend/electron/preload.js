const { contextBridge, ipcRenderer } = require('electron')
const runtimeConfig = require('../config/runtime.json')

const backendBaseUrl = `http://${runtimeConfig.backendHost || '127.0.0.1'}:${runtimeConfig.backendPort || 8420}`

contextBridge.exposeInMainWorld('electronAPI', {
  storeGet: (key) => ipcRenderer.invoke('store:get', key),
  storeSet: (key, value) => ipcRenderer.invoke('store:set', key, value),
  storeDelete: (key) => ipcRenderer.invoke('store:delete', key),
  selectDirectory: (defaultPath) => ipcRenderer.invoke('dialog:select-directory', defaultPath),
  setTheme: (theme) => ipcRenderer.invoke('theme:set', theme),
  backendBaseUrl,
  isElectron: true,
})
