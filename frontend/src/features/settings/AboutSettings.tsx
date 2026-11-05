import { useEffect, useState } from 'react'
import { PageHeader, SettingsCard, SettingRow } from '../../components/ui/Panel'

export function AboutSettings() {
  const [update, setUpdate] = useState<UpdateState | null>(null)
  const [confirmRestart, setConfirmRestart] = useState(false)

  useEffect(() => {
    let active = true
    let receivedEvent = false
    const api = window.electronAPI
    const unsubscribe = api?.onUpdateState?.((state) => {
      receivedEvent = true
      if (active) setUpdate(state)
    })
    void api?.updateStatus?.().then((state) => {
      if (active && !receivedEvent) setUpdate(state)
    }).catch(() => {
      if (active) setUpdate({ currentVersion: '', status: 'error', error: 'Could not get update status.' })
    })
    return () => { active = false; unsubscribe?.() }
  }, [])

  const check = async () => {
    if (!window.electronAPI?.checkForUpdates) return
    try { setUpdate(await window.electronAPI.checkForUpdates()) }
    catch { setUpdate((current) => ({ currentVersion: current?.currentVersion || '', status: 'error', error: 'Could not check for updates.' })) }
  }
  const install = async () => {
    try {
      if (!(await window.electronAPI?.installUpdate?.())) {
        setUpdate((current) => ({ currentVersion: current?.currentVersion || '', status: 'error', error: 'The update is not ready. Please check again.' }))
      }
    } catch {
      setUpdate((current) => ({ currentVersion: current?.currentVersion || '', status: 'error', error: 'Could not start the update. Please try again.' }))
    }
    setConfirmRestart(false)
  }
  const status = update?.status
  const message = !window.electronAPI?.updateStatus ? 'Updates are available in the Windows desktop app.'
    : !update ? 'Loading update status…'
      : status === 'unavailable' ? 'Update checks are available in the installed Windows app.'
        : status === 'checking' ? 'Checking for updates…'
          : status === 'up-to-date' ? 'You are up to date.'
            : status === 'downloading' ? `Downloading version ${update.latestVersion || ''} (${update.progress || 0}%).`
              : status === 'ready' ? `Version ${update.latestVersion || ''} is ready to install.`
                : status === 'error' ? update.error || 'Could not check for updates.'
                  : 'Monaw checks for updates automatically.'

  return <>
    <PageHeader title="About" />
    <SettingsCard title="Monaw">
      <SettingRow label="Version" description="Installed desktop app version.">
        <span>{update?.currentVersion || '—'}</span>
      </SettingRow>
      <div className="st-row flex flex-wrap items-center justify-between gap-3">
        <div><p className="st-label">Software updates</p><p role="status" aria-live="polite" className="st-desc mt-1">{message}</p></div>
        {status === 'ready'
          ? <button type="button" className="st-btn st-btn-secondary" onClick={() => setConfirmRestart(true)}>Restart to update</button>
          : <button type="button" className="st-btn st-btn-secondary" onClick={() => void check()}
              disabled={!update || ['unavailable', 'checking', 'downloading'].includes(status || '')}>Check for updates</button>}
      </div>
      {confirmRestart && <div className="st-row flex flex-wrap items-center gap-3" role="group" aria-label="Confirm update restart">
        <p className="st-desc flex-1">Monaw will close and restart to install the update. Finish or pause active work first. Updates are currently unsigned, so their publisher cannot be verified.</p>
        <button type="button" className="st-btn st-btn-secondary" onClick={() => setConfirmRestart(false)}>Cancel</button>
        <button type="button" className="st-btn st-btn-secondary" onClick={() => void install()}>Restart and install</button>
      </div>}
    </SettingsCard>
  </>
}
