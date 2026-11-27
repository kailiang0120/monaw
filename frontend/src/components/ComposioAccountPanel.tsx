import { useEffect, useRef, useState, type ReactNode } from 'react'
import { ExternalLink, Loader2 } from 'lucide-react'
import { connectorRequest, type ConnectorAccount } from '../lib/api/connectors'
import { Badge, InfoTip, Note, SettingsCard } from './ui/Panel'

interface Login {
  login_id: string
  authorization_url: string
  interval: number
  expires_in: number
}

/** Accept only the selected provider's exact HTTPS origin before opening the browser. */
function trustedAuthorization(value: string): boolean {
  try {
    const url = new URL(value)
    const trusted = 'https://connect.composio.dev'
    if (url.username || url.password || url.protocol !== 'https:') return false
    const OriginAPI = (
      globalThis as typeof globalThis & {
        Origin?: { from: (value: string) => { isSameOrigin: (other: unknown) => boolean } }
      }
    ).Origin
    return OriginAPI
      ? OriginAPI.from(url.href).isSameOrigin(OriginAPI.from(trusted))
      : url.origin === trusted
  } catch {
    return false
  }
}

/** Open managed sign-in while Monaw retains local account permissions. */
export function ComposioAccountPanel({
  providerId,
  providerName,
  setupNotice,
  icon,
  description,
  configured,
  accounts,
  onChanged,
}: {
  providerId: string
  providerName?: string
  setupNotice?: string
  icon?: ReactNode
  description?: string
  configured: boolean
  accounts: ConnectorAccount[]
  onChanged: () => void
}) {
  const [login, setLogin] = useState<Login | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const current = useRef(0)
  const pending = useRef<string | null>(null)
  const continueLink = useRef<HTMLAnchorElement>(null)
  const name =
    providerName ??
    (providerId === 'gmail' ? 'Gmail' : providerId === 'outlook' ? 'Outlook' : providerId)

  /** Invalidate pending callbacks before removing the server-owned linking attempt. */
  function cancel() {
    current.current += 1
    const id = pending.current
    pending.current = null
    setLogin(null)
    setBusy(false)
    if (id) void connectorRequest(`/composio/login/${id}`, 'DELETE').catch(() => undefined)
  }

  useEffect(
    () => () => {
      current.current += 1
      if (pending.current)
        void connectorRequest(`/composio/login/${pending.current}`, 'DELETE').catch(() => undefined)
      pending.current = null
    },
    [],
  )

  useEffect(() => {
    if (!login) return
    continueLink.current?.focus()
    let stopped = false
    let timer: ReturnType<typeof setTimeout>
    const generation = current.current
    const deadline = Date.now() + login.expires_in * 1000
    /** Honor server pacing and discard responses after cancellation or panel dismissal. */
    async function poll() {
      if (stopped || current.current !== generation) return
      if (Date.now() >= deadline) {
        cancel()
        setError('Sign-in expired. Please try again.')
        return
      }
      try {
        const result = await connectorRequest<{ status: string; interval?: number }>(
          `/composio/login/${login!.login_id}/poll`,
          'POST',
        )
        if (stopped || current.current !== generation) return
        if (result.status === 'connected') {
          pending.current = null
          setLogin(null)
          setNotice(`${name} connected.`)
          onChanged()
        } else {
          timer = setTimeout(() => void poll(), Math.max(2, result.interval || 2) * 1000)
        }
      } catch {
        if (!stopped && current.current === generation) {
          cancel()
          setError('Sign-in could not be completed. Please try again.')
        }
      }
    }
    timer = setTimeout(() => void poll(), Math.max(2, login.interval) * 1000)
    return () => {
      stopped = true
      clearTimeout(timer)
    }
  }, [login])

  /** Start sign-in in the system browser; retain a normal link if popups are blocked. */
  async function connect(account?: ConnectorAccount) {
    const generation = ++current.current
    setBusy(true)
    setError('')
    setNotice('')
    try {
      const result = await connectorRequest<Login>('/composio/login', 'POST', {
        provider_id: providerId,
        ...(account ? { account_id: account.id, expected_revision: account.revision } : {}),
      })
      if (generation !== current.current) {
        void connectorRequest(`/composio/login/${result.login_id}`, 'DELETE').catch(() => undefined)
        return
      }
      pending.current = result.login_id
      if (!trustedAuthorization(result.authorization_url)) {
        cancel()
        setError('The sign-in address was invalid. Please try again.')
        return
      }
      setLogin(result)
      window.open(result.authorization_url, '_blank', 'noopener,noreferrer')
    } catch {
      if (generation === current.current)
        setError('Could not start sign-in. Restart Monaw and try again.')
    } finally {
      if (generation === current.current) setBusy(false)
    }
  }

  /** Disconnect locally without revoking unrelated permissions at the provider. */
  async function disconnect(account: ConnectorAccount, remove = false) {
    const generation = current.current
    setBusy(true)
    setError('')
    try {
      await connectorRequest(
        `/accounts/${account.id}${remove ? '' : '/disconnect'}`,
        remove ? 'DELETE' : 'POST',
        {
          expected_revision: account.revision,
        },
      )
      if (generation === current.current) {
        setNotice(remove ? 'Account removed.' : 'Account disconnected.')
        onChanged()
      }
    } catch {
      if (generation === current.current)
        setError('Could not disconnect this account. Refresh and try again.')
    } finally {
      if (generation === current.current) setBusy(false)
    }
  }

  return (
    <SettingsCard>
      <div className="st-card-head flex flex-wrap items-center justify-between gap-3">
        <div className="flex min-w-0 items-center gap-3">
          {icon && <span className="st-connector-icon">{icon}</span>}
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <h4 className="st-label truncate">{name}</h4>
              <Badge>Read-only tools</Badge>
            </div>
            {description && <p className="st-desc mt-0.5">{description}</p>}
          </div>
        </div>
        {!login && (
          <button
            type="button"
            disabled={busy || !configured || !!setupNotice}
            onClick={() => void connect()}
            className="st-btn st-btn-primary shrink-0"
          >
            {busy ? 'Connecting…' : `Connect ${name}`}
          </button>
        )}
      </div>

      {(!configured || setupNotice || login || notice || error) && (
        <div className="space-y-3 p-4">
          {setupNotice && (
            <p className="st-desc flex items-center gap-2">
              Sign-in unavailable
              <InfoTip label="About Sign-in unavailable" description={setupNotice} inline />
            </p>
          )}
          {!configured && !setupNotice && (
            <Note tone="warn">Add your Composio API key below to connect.</Note>
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
          {login && (
            <div className="st-connector-code-box space-y-3">
              <p className="st-desc">
                Finish signing in in your browser. This window will update automatically.
              </p>
              <div className="flex flex-wrap items-center gap-2">
                <a
                  ref={continueLink}
                  href={login.authorization_url}
                  target="_blank"
                  rel="noreferrer"
                  className="st-btn st-btn-primary"
                >
                  <ExternalLink size={13} aria-hidden="true" />
                  Continue to {name}
                </a>
                <button type="button" onClick={cancel} className="st-btn st-btn-ghost">
                  Cancel
                </button>
              </div>
              <p role="status" className="st-desc flex items-center gap-1.5">
                <Loader2 size={12} className="animate-spin" aria-hidden="true" />
                Waiting for {name} authorization…
              </p>
            </div>
          )}
        </div>
      )}

      {accounts.length > 0 && (
        <ul className="st-divider-t list-none" role="list">
          {accounts.map((account) => {
            const badgeTone =
              account.state === 'ready'
                ? 'ok'
                : account.state === 'needs_reauth'
                  ? 'warn'
                  : 'neutral'
            const stateText =
              account.state === 'ready'
                ? 'Connected'
                : account.state === 'needs_reauth'
                  ? 'Sign in again'
                  : 'Disconnected'
            return (
              <li key={account.id} className="st-row flex-wrap gap-3">
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <p className="st-label break-words">{account.label}</p>
                    <Badge tone={badgeTone}>{stateText}</Badge>
                  </div>
                </div>
                <div className="st-row-control-auto flex items-center gap-1.5">
                  <button
                    type="button"
                    className={`st-btn ${account.state === 'ready' ? 'st-btn-secondary' : 'st-btn-primary'}`}
                    disabled={
                      busy ||
                      !!login ||
                      (account.state !== 'ready' && (!configured || !!setupNotice))
                    }
                    aria-label={`${account.state === 'ready' ? 'Disconnect' : 'Reconnect'} ${account.label}`}
                    onClick={() =>
                      account.state === 'ready' ? void disconnect(account) : void connect(account)
                    }
                  >
                    {account.state === 'ready' ? 'Disconnect' : 'Reconnect'}
                  </button>
                  {account.state !== 'ready' && (
                    <button
                      type="button"
                      className="st-btn st-btn-ghost"
                      disabled={busy || !!login}
                      aria-label={`Remove ${account.label}`}
                      onClick={() => void disconnect(account, true)}
                    >
                      Remove
                    </button>
                  )}
                </div>
              </li>
            )
          })}
        </ul>
      )}
    </SettingsCard>
  )
}
