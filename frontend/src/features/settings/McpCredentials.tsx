import { useEffect, useRef, useState } from 'react'
import type { MCPServerConfig } from '../../lib/api/types'

export function McpCredentials({ server, disabled, onChanged }: {
  server: MCPServerConfig
  disabled: boolean
  onChanged: () => Promise<void>
}) {
  const [key, setKey] = useState('')
  const [value, setValue] = useState('')
  const [pending, setPending] = useState(false)
  const [feedback, setFeedback] = useState('')
  const alive = useRef(true)
  const inFlight = useRef(false)
  useEffect(() => { alive.current = true; return () => { alive.current = false } }, [])
  const target = server.transport === 'stdio' ? 'env' : 'headers'
  const storedKeys = Object.keys(server.credential_refs?.[target] || {})
  const legacyKeys = [...new Set([...(server.legacy_secret_fields?.[target] || []), ...Object.keys(server[target])])]
  const unavailable = disabled || !server.server_id || !window.electronAPI?.changeMcpCredential

  async function change(action: 'migrate' | 'replace' | 'delete', selectedKey: string) {
    if (unavailable || inFlight.current || !server.server_id) return
    inFlight.current = true
    setPending(true)
    setFeedback('')
    try {
      await window.electronAPI!.changeMcpCredential({ action, binding: {
        server_id: server.server_id, transport: server.transport, target, key: selectedKey,
      }, ...(action === 'replace' ? { value } : {}) })
      if (alive.current) setValue('')
      await onChanged()
      if (alive.current) setFeedback(action === 'delete'
        ? 'Credential deleted. This connection will wait until a credential is restored.'
        : 'Credential stored with OS encryption and applied to this connection.')
    } catch {
      // Never display an OS/transport exception that could echo an entered value.
      if (alive.current) setFeedback('Credential change could not complete. Check OS encryption and saved settings, then retry to reconcile.')
    } finally {
      inFlight.current = false
      if (alive.current) setPending(false)
    }
  }

  return <section className="st-row-stacked" aria-label={`Credentials for ${server.name}`}>
    <p className="st-label">Encrypted credentials</p>
    <p className="st-hint">Changes apply immediately to the saved server and reconnect it. Save configuration changes first. Stored values are never read back here.</p>
    {!window.electronAPI?.changeMcpCredential && <p className="st-hint">Use the desktop app, or the documented authenticated headless credential handoff.</p>}
    <fieldset disabled={unavailable || pending} className="space-y-2">
      {legacyKeys.map(legacyKey => <div key={legacyKey} className="flex items-center justify-between gap-2">
        <span className="st-hint">{legacyKey} · plaintext configuration</span>
        <button type="button" className="st-btn st-btn-secondary" onClick={() => void change('migrate', legacyKey)}>Encrypt {legacyKey}</button>
      </div>)}
      {storedKeys.map(storedKey => <div key={storedKey} className="flex items-center justify-between gap-2">
        <span className="st-hint">{storedKey} · encrypted reference</span>
        <button type="button" className="st-btn st-btn-ghost" onClick={() => void change('delete', storedKey)}>Delete credential {storedKey}</button>
      </div>)}
      <label className="st-hint block">{target === 'env' ? 'Credential environment variable' : 'Credential HTTP header'}
        <input className="st-input" value={key} onChange={e => setKey(e.target.value)} placeholder={target === 'env' ? 'API_KEY' : 'Authorization'} />
      </label>
      <label className="st-hint block">New credential value
        <input type="password" autoComplete="new-password" className="st-input" value={value} onChange={e => setValue(e.target.value)} />
      </label>
      <button type="button" className="st-btn st-btn-secondary" disabled={!/^[A-Za-z_][A-Za-z0-9_.-]{0,127}$/.test(key) || !value}
        onClick={() => void change('replace', key)}>Store or replace credential</button>
    </fieldset>
    {pending && <p role="status" className="st-hint">Applying credential…</p>}
    {feedback && <p role="status" className="st-hint">{feedback}</p>}
  </section>
}
