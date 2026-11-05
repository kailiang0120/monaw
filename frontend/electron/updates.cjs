// All network and installer operations stay in the main process. The renderer
// can request a check or a restart, but cannot choose an update URL or file.
function createUpdateController({ updater, version, enabled, installDirectory, publish = () => {} }) {
  let state = { currentVersion: version, status: enabled ? 'idle' : 'unavailable' }
  let checking = null
  const setState = (next) => {
    state = { ...state, ...next }
    publish({ ...state })
  }
  if (enabled) {
    updater.autoDownload = true
    updater.autoInstallOnAppQuit = false
    updater.allowPrerelease = false
    updater.allowDowngrade = false
    if (installDirectory) updater.installDirectory = installDirectory
    updater.on('checking-for-update', () => setState({ status: 'checking', error: undefined }))
    updater.on('update-available', (info) => setState({ status: 'downloading', latestVersion: info.version, progress: 0, error: undefined }))
    updater.on('update-not-available', () => setState({ status: 'up-to-date', latestVersion: undefined, progress: undefined, error: undefined }))
    updater.on('download-progress', (progress) => setState({ status: 'downloading', progress: Math.max(0, Math.min(100, Math.round(progress.percent || 0))) }))
    updater.on('update-downloaded', (info) => setState({ status: 'ready', latestVersion: info.version, progress: 100, error: undefined }))
    updater.on('error', () => setState({ status: 'error', error: 'Could not check for or download the update. Please try again.' }))
  }
  return {
    getState: () => ({ ...state }),
    async check() {
      if (!enabled || state.status === 'ready' || state.status === 'downloading') return { ...state }
      if (!checking) {
        setState({ status: 'checking', error: undefined })
        checking = Promise.resolve().then(() => updater.checkForUpdates())
          .then((result) => {
            // electron-updater reports downloads separately from the check.
            // Consume rejection as well as listening for its error event.
            if (result?.downloadPromise) void result.downloadPromise.catch(() => {
              setState({ status: 'error', error: 'Could not check for or download the update. Please try again.' })
            })
          })
          .catch(() => setState({ status: 'error', error: 'Could not check for or download the update. Please try again.' }))
          .finally(() => { checking = null })
      }
      await checking
      return { ...state }
    },
    install() {
      if (!enabled || state.status !== 'ready') return false
      // The user already confirmed in-app; NSIS must not open a second wizard.
      updater.quitAndInstall(true, true)
      return true
    },
  }
}

module.exports = { createUpdateController }
