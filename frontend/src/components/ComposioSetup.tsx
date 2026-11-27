import { useEffect, useRef, useState } from 'react'
import { ChevronDown, ExternalLink } from 'lucide-react'
import { connectorRequest } from '../lib/api/connectors'
import { Badge, Note, SettingsCard } from './ui/Panel'

const DASHBOARD = 'https://dashboard.composio.dev/'

/** Configure a user-owned Platform key once, then let Monaw provision connector sign-in. */
export function ComposioSetup({ onChanged }: { onChanged: (configured: boolean) => void }) {
  const [configured, setConfigured] = useState(false)
  const [editing, setEditing] = useState(false)
  const [key, setKey] = useState('')
  const [busy, setBusy] = useState(true)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const mounted = useRef(false)
  const notify = useRef(onChanged)
  notify.current = onChanged

  useEffect(() => {
    mounted.current = true
    void connectorRequest<{ configured: boolean }>('/composio/configuration')
      .then((result) => {
        if (!mounted.current) return
        setConfigured(result.configured)
        notify.current(result.configured)
      })
      .catch(() => {
        if (mounted.current)
          setError('Could not load Composio settings. Reopen this panel to retry.')
      })
      .finally(() => {
        if (mounted.current) setBusy(false)
      })
    return () => {
      mounted.current = false
    }
  }, [])

  /** Hand off a transient input through Electron; never read saved credentials into the renderer. */
  async function update(action: 'save' | 'remove') {
    const value = key.trim()
    setKey('')
    setBusy(true)
    setError('')
    setNotice('')
    try {
      if (!window.electronAPI?.changeComposioKey)
        throw new Error('Open Monaw desktop to manage your API key.')
      await window.electronAPI.changeComposioKey({
        action,
        ...(action === 'save' ? { value } : {}),
      })
      if (!mounted.current) return
      const enabled = action === 'save'
      setConfigured(enabled)
      setEditing(false)
      setNotice(
        enabled
          ? 'Key saved. Choose an app below to connect.'
          : 'Key removed. Connected mail accounts are disabled in Monaw.',
      )
      notify.current(enabled)
    } catch {
      if (mounted.current)
        setError(
          'Could not update Composio. Use a Platform project API key and check its permissions. Key changes require Monaw desktop.',
        )
    } finally {
      if (mounted.current) setBusy(false)
    }
  }

  return (
    <SettingsCard>
      <div className="st-card-head flex flex-wrap items-center justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h4 className="st-label">Composio</h4>
            <Badge tone={configured ? 'ok' : 'neutral'}>
              <span className={configured ? 'st-dot st-dot-ok' : 'st-dot'} aria-hidden="true" />
              {configured ? 'Configured' : 'Set up once'}
            </Badge>
          </div>
          <p className="st-desc mt-0.5">Connect your everyday apps.</p>
        </div>

        <div className="flex flex-wrap items-center gap-1.5">
          <a
            href={DASHBOARD}
            target="_blank"
            rel="noopener noreferrer"
            className="st-btn st-btn-ghost st-btn-sm"
          >
            <ExternalLink size={13} aria-hidden="true" />
            Open Composio
          </a>
          {configured && !editing && (
            <>
              <button
                type="button"
                className="st-btn st-btn-secondary st-btn-sm"
                disabled={busy}
                onClick={() => { setEditing(true); setError('') }}
              >
                Change key
              </button>
              <button
                type="button"
                className="st-btn st-btn-danger st-btn-sm"
                disabled={busy}
                onClick={() => void update('remove')}
              >
                Remove key
              </button>
            </>
          )}
        </div>
      </div>

      {(!configured || editing || notice || error) && (
        <div className="space-y-3 p-4">
          {(!configured || editing) && (
            <form
              className="space-y-2"
              onSubmit={(event) => {
                event.preventDefault()
                void update('save')
              }}
            >
              <label className="st-label block" htmlFor="composio-project-key">
                Platform API key
              </label>
              <div className="flex flex-wrap gap-2">
                <input
                  id="composio-project-key"
                  type="password"
                  autoComplete="off"
                  spellCheck={false}
                  className="st-input min-w-0 flex-1"
                  value={key}
                  maxLength={4096}
                  disabled={busy}
                  onChange={(event) => setKey(event.target.value)}
                  placeholder="Paste your project API key"
                />
                <button
                  type="submit"
                  className="st-btn st-btn-primary"
                  disabled={busy || !key.trim()}
                >
                  {busy ? 'Checking…' : 'Save key'}
                </button>
                {editing && (
                  <button
                    type="button"
                    className="st-btn st-btn-ghost"
                    disabled={busy}
                    onClick={() => { setEditing(false); setKey(''); setError('') }}
                  >
                    Cancel
                  </button>
                )}
              </div>
            </form>
          )}

          {notice && (
            <p role="status" className="st-desc">
              {notice}
            </p>
          )}

          {error && (
            <div role="alert">
              <Note tone="danger">{error}</Note>
            </div>
          )}
        </div>
      )}

      <details className="st-obs-disclosure st-divider-t">
        <summary className="flex items-center justify-between gap-2 px-4 py-2 text-xs font-medium hover:bg-[var(--st-surface-hover)]">
          <span style={{ color: 'var(--st-text-muted)' }}>Setup guide</span>
          <ChevronDown
            size={13}
            className="st-obs-disclosure-chevron shrink-0"
            style={{ color: 'var(--st-text-faint)' }}
            aria-hidden="true"
          />
        </summary>
        <div className="st-divider-t space-y-2 px-4 py-3 text-xs" style={{ color: 'var(--st-text-muted)' }}>
          <ol className="list-decimal space-y-1 pl-5 leading-relaxed">
            <li>Open Composio, sign up, and select Platform.</li>
            <li>Create a project. Open its Settings → API Keys and create a project key.</li>
            <li>
              Save it here, then choose a connector. Monaw prepares sign-in automatically.
            </li>
          </ol>
          <p className="st-hint">
            Your Composio plan covers your usage. The key is encrypted on this device; provider
            credentials and connector requests are handled by Composio. Review the browser consent
            screen before approving access.
          </p>
        </div>
      </details>
    </SettingsCard>
  )
}
