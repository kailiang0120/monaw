import { useEffect, useRef, useState } from 'react'
import {
  ArrowLeft,
  Check,
  ChevronRight,
  Copy,
  ExternalLink,
  Loader2,
  Plug,
  Search,
  X,
} from 'lucide-react'
import { createPortal } from 'react-dom'
import { useModalFocus } from '../hooks/useModalFocus'
import { ApiError } from '../lib/api/client'
import {
  connectorRequest,
  type ConnectorAccount,
  type ConnectorLogin,
  type ConnectorProvider,
} from '../lib/api/connectors'
import { Badge, Note, SettingsCard, SwitchRow } from './ui/Panel'
import { ComposioAccountPanel } from './ComposioAccountPanel'
import { ComposioSetup } from './ComposioSetup'
import { ConnectorIcon as ProviderIcon } from './ConnectorIcon'
import { Dropdown } from './Dropdown'

const PROVIDER_SUMMARY: Record<string, string> = {
  github: 'Repositories, issues, pull requests, and workflows.',
  gmail: 'Find and read Gmail messages.',
  outlook: 'Read Outlook mail and browse folders.',
}

/** Translate provider failures into useful recovery instructions. */
function failure(error: unknown): string {
  const messages: Record<string, string> = {
    authorization_expired: 'The sign-in code expired. Start sign-in again.',
    authorization_invalid: 'Sign-in was cancelled or could not be verified. Try again.',
    account_exists: 'This GitHub account is already connected.',
    account_changed: 'This connection changed. Refresh and try again.',
    rate_limited: 'GitHub is receiving too many requests. Wait a moment and try again.',
    credentials_unavailable: 'Secure credential storage is unavailable on this device.',
    provider_unavailable: 'GitHub sign-in is not configured for this build.',
  }
  return error instanceof ApiError
    ? messages[error.code] || 'Could not update the connection. Try again.'
    : 'Could not reach Monaw. Check that the app is running, then retry.'
}

const CATEGORIES = [
  ['email', 'Email'],
  ['calendar', 'Calendars'],
  ['files', 'Files & documents'],
  ['knowledge', 'Notes & knowledge'],
  ['messaging', 'Messaging'],
  ['tasks', 'Tasks & projects'],
  ['development', 'Development'],
  ['other', 'Other'],
] as const

/** Keep older provider responses usable while new metadata supplies the category. */
function providerCategory(provider: ConnectorProvider): string {
  const category =
    provider.category ??
    (provider.id === 'github'
      ? 'development'
      : ['gmail', 'outlook'].includes(provider.id)
        ? 'email'
        : 'other')
  return CATEGORIES.some(([id]) => id === category) ? category : 'other'
}

/** Detect managed providers without treating every future native adapter as Composio. */
function usesComposio(provider: ConnectorProvider): boolean {
  return provider.connection_method === 'composio' || ['gmail', 'outlook'].includes(provider.id)
}

/** Prefer the adapter's description over client-side legacy copy. */
function providerSummary(provider: ConnectorProvider): string {
  return (
    provider.description ||
    PROVIDER_SUMMARY[provider.id] ||
    `${provider.capabilities.length} available action${provider.capabilities.length === 1 ? '' : 's'}.`
  )
}

function providerStatus(providerAccounts: ConnectorAccount[]): {
  label: string
  tone: 'neutral' | 'ok' | 'warn'
  dotClass: string
} {
  if (providerAccounts.length === 0) {
    return { label: 'Not connected', tone: 'neutral', dotClass: 'st-dot' }
  }
  const readyCount = providerAccounts.filter((account) => account.state === 'ready').length
  if (readyCount > 0) {
    return {
      label: readyCount === 1 ? 'Connected' : `${readyCount} connected`,
      tone: 'ok',
      dotClass: 'st-dot st-dot-ok',
    }
  }
  if (providerAccounts.some((account) => account.state === 'needs_reauth')) {
    return { label: 'Sign in again', tone: 'warn', dotClass: 'st-dot st-dot-warn' }
  }
  return { label: 'Disconnected', tone: 'neutral', dotClass: 'st-dot' }
}

function accountStateLabel(state: string): { label: string; tone: 'ok' | 'warn' | 'neutral' } {
  if (state === 'ready') return { label: 'Connected', tone: 'ok' }
  if (state === 'needs_reauth') return { label: 'Sign in again', tone: 'warn' }
  return { label: state.replace(/_/g, ' '), tone: 'neutral' }
}

/** Manage any registered connector; provider-specific sign-in remains isolated here. */
export function ConnectorPanel() {
  const [composioConfigured, setComposioConfigured] = useState(false)
  const [providers, setProviders] = useState<ConnectorProvider[]>([])
  const [accounts, setAccounts] = useState<ConnectorAccount[]>([])
  const [config, setConfig] = useState({ device_login_available: false, installation_url: '' })
  const [selectedProviderId, setSelectedProviderId] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [search, setSearch] = useState('')
  const [category, setCategory] = useState('all')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [login, setLogin] = useState<ConnectorLogin | null>(null)
  const [copiedCode, setCopiedCode] = useState(false)
  const [copyCodeError, setCopyCodeError] = useState('')

  const mounted = useRef(false)
  const generation = useRef(0)
  const pending = useRef<string | null>(null)
  const continueLink = useRef<HTMLAnchorElement>(null)
  const backButton = useRef<HTMLButtonElement>(null)
  const cardRefs = useRef<Record<string, HTMLButtonElement | null>>({})
  const lastOpenedProvider = useRef<string | null>(null)
  const wasInSetup = useRef(false)
  const copyTimer = useRef<ReturnType<typeof setTimeout> | null>(null)

  function clearCopyState() {
    if (copyTimer.current) {
      clearTimeout(copyTimer.current)
      copyTimer.current = null
    }
    setCopiedCode(false)
    setCopyCodeError('')
  }

  /** Reload metadata only; tokens never reach this component. */
  async function refresh(silent = false) {
    if (!silent) setLoading(true)
    setError('')
    try {
      const [catalog, saved, github, composio] = await Promise.all([
        connectorRequest<{ providers: ConnectorProvider[] }>('/providers'),
        connectorRequest<{ accounts: ConnectorAccount[] }>('/accounts'),
        connectorRequest<typeof config>('/github/configuration'),
        connectorRequest<{ configured?: boolean }>('/composio/configuration').catch(() => ({
          configured: false,
        })),
      ])
      if (!mounted.current) return
      setProviders(catalog.providers)
      setAccounts(saved.accounts)
      setConfig(github)
      setComposioConfigured(Boolean(composio?.configured))
    } catch (reason) {
      if (mounted.current) setError(failure(reason))
    } finally {
      if (mounted.current) setLoading(false)
    }
  }

  /** Cancel the server attempt as well as the visible polling state. */
  function cancel() {
    generation.current += 1
    const id = pending.current
    pending.current = null
    setLogin(null)
    setBusy(false)
    clearCopyState()
    if (id) void connectorRequest(`/github/login/${id}`, 'DELETE').catch(() => undefined)
  }

  useEffect(() => {
    mounted.current = true
    void refresh()
    return () => {
      mounted.current = false
      generation.current += 1
      if (copyTimer.current) clearTimeout(copyTimer.current)
      if (pending.current)
        void connectorRequest(`/github/login/${pending.current}`, 'DELETE').catch(() => undefined)
      pending.current = null
    }
  }, [])

  useEffect(() => {
    if (selectedProviderId) {
      wasInSetup.current = true
      if (!login) backButton.current?.focus()
    } else if (wasInSetup.current) {
      wasInSetup.current = false
      const target = lastOpenedProvider.current
      if (target) cardRefs.current[target]?.focus()
    }
  }, [selectedProviderId, login])

  useEffect(() => {
    clearCopyState()
    if (!login) return
    continueLink.current?.focus()
    let stopped = false
    let timer: ReturnType<typeof setTimeout>
    const current = generation.current
    const deadline = Date.now() + login.expires_in * 1000
    /** Poll only at GitHub's requested interval, with no overlapping exchanges. */
    async function poll() {
      if (stopped || generation.current !== current) return
      try {
        if (Date.now() >= deadline) {
          cancel()
          setError('The sign-in code expired. Start sign-in again.')
          return
        }
        const result = await connectorRequest<{ status: string; interval?: number }>(
          `/github/login/${login!.login_id}/poll`,
          'POST',
        )
        if (stopped || generation.current !== current) return
        if (result.status === 'connected') {
          pending.current = null
          setLogin(null)
          clearCopyState()
          setNotice('GitHub connected.')
          void refresh()
        } else {
          timer = setTimeout(
            () => void poll(),
            Math.max(1, result.interval || login!.interval) * 1000,
          )
        }
      } catch (reason) {
        if (!stopped && generation.current === current) {
          cancel()
          setError(failure(reason))
        }
      }
    }
    timer = setTimeout(() => void poll(), login.interval * 1000)
    return () => {
      stopped = true
      clearTimeout(timer)
    }
  }, [login])

  /** Begin browser sign-in, preserving explicit write choices on reconnect. */
  async function connect(account?: ConnectorAccount) {
    const current = ++generation.current
    setBusy(true)
    setError('')
    setNotice('')
    clearCopyState()
    try {
      const provider = providers.find((item) => item.id === 'github')
      const result = await connectorRequest<ConnectorLogin>('/github/login', 'POST', {
        ...(account ? { account_id: account.id, revision: account.revision } : {}),
        allow_writes:
          !!account &&
          !!provider?.capabilities.some(
            (cap) => cap.action !== 'read' && account.enabled_capabilities.includes(cap.id),
          ),
      })
      if (!mounted.current || generation.current !== current) {
        void connectorRequest(`/github/login/${result.login_id}`, 'DELETE').catch(() => undefined)
        return
      }
      pending.current = result.login_id
      setLogin(result)
    } catch (reason) {
      if (mounted.current && generation.current === current) setError(failure(reason))
    } finally {
      if (mounted.current && generation.current === current) setBusy(false)
    }
  }

  /** Save account permissions or disconnect using the displayed revision. */
  async function change(
    account: ConnectorAccount,
    action: 'writes' | 'disconnect',
    allowWrites = false,
  ) {
    setBusy(true)
    setError('')
    try {
      const provider = providers.find((item) => item.id === account.provider_id)!
      await connectorRequest(
        `/accounts/${account.id}${action === 'disconnect' ? '/disconnect' : ''}`,
        action === 'disconnect' ? 'POST' : 'PATCH',
        {
          expected_revision: account.revision,
          ...(action === 'writes'
            ? {
                label: account.label,
                enabled: account.enabled,
                enabled_capabilities: provider.capabilities
                  .filter((cap) => allowWrites || cap.action === 'read')
                  .map((cap) => cap.id),
              }
            : {}),
        },
      )
      if (mounted.current) await refresh()
    } catch (reason) {
      if (mounted.current) setError(failure(reason))
    } finally {
      if (mounted.current) setBusy(false)
    }
  }

  async function copyCode() {
    if (!login) return
    clearCopyState()
    try {
      if (window.electronAPI?.writeClipboardText) {
        await window.electronAPI.writeClipboardText(login.user_code)
      } else {
        await navigator.clipboard.writeText(login.user_code)
      }
      if (!mounted.current) return
      setCopiedCode(true)
      copyTimer.current = setTimeout(() => {
        if (mounted.current) setCopiedCode(false)
      }, 2500)
    } catch {
      if (!mounted.current) return
      setCopiedCode(false)
      setCopyCodeError(
        'Could not copy automatically. Select the code and press Ctrl+C (⌘C on Mac).',
      )
    }
  }

  function openProvider(providerId: string) {
    lastOpenedProvider.current = providerId
    setError('')
    setNotice('')
    setSelectedProviderId(providerId)
  }

  function closeProvider() {
    if (login || pending.current) cancel()
    setError('')
    setNotice('')
    setSelectedProviderId(null)
  }

  const selectedProvider = selectedProviderId
    ? (providers.find((provider) => provider.id === selectedProviderId) ?? null)
    : null
  const providerAccounts = selectedProvider
    ? accounts.filter((account) => account.provider_id === selectedProvider.id)
    : []

  const hasComposioProviders = providers.some(usesComposio)

  return (
    <div className="space-y-3 text-[var(--st-text)]">
      {error && (
        <div role="alert">
          <Note tone="danger">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <span>{error}</span>
              <button
                type="button"
                className="st-btn st-btn-ghost st-btn-sm"
                onClick={() => void refresh()}
                disabled={busy}
              >
                Refresh
              </button>
            </div>
          </Note>
        </div>
      )}

      {notice && (
        <div role="status">
          <Note tone="info">{notice}</Note>
        </div>
      )}

      {loading ? (
        <div className="st-card px-4 py-8 text-center">
          <p role="status" className="st-desc mx-auto inline-flex items-center gap-2">
            <Loader2 size={14} className="animate-spin" aria-hidden="true" />
            Loading connections…
          </p>
        </div>
      ) : providers.length === 0 && !error ? (
        <div className="st-card px-4 py-8 text-center">
          <p className="st-desc mx-auto">No connectors are available in this build.</p>
        </div>
      ) : selectedProvider ? (
        <div className="space-y-3">
          <div className="flex items-center justify-between gap-2">
            <button
              ref={backButton}
              type="button"
              onClick={closeProvider}
              className="st-btn st-btn-ghost st-btn-sm"
            >
              <ArrowLeft size={13} aria-hidden="true" />
              All connectors
            </button>
          </div>

          {selectedProvider.id === 'github' && (
            <SettingsCard>
              <div className="st-card-head flex flex-wrap items-center justify-between gap-3">
                <div className="flex min-w-0 items-center gap-3">
                  <span className="st-connector-icon">
                    <ProviderIcon providerId={selectedProvider.id} />
                  </span>
                  <div className="min-w-0">
                    <h4 className="st-label truncate">{selectedProvider.name}</h4>
                    <p className="st-desc mt-0.5">{providerSummary(selectedProvider)}</p>
                  </div>
                </div>
                <button
                  type="button"
                  className="st-btn st-btn-primary shrink-0"
                  disabled={busy || !!login || !config.device_login_available}
                  onClick={() => void connect()}
                >
                  Connect GitHub
                </button>
              </div>

              {!config.device_login_available && !login && (
                <div className="p-4">
                  <Note tone="warn">GitHub sign-in is not configured for this build.</Note>
                </div>
              )}

              {config.installation_url && (
                <div className="st-row flex-wrap">
                  <div className="min-w-0">
                    <p className="st-label">Repository access</p>
                    <p className="st-desc mt-1">
                      Choose which repositories Monaw Agent can read or update.
                    </p>
                  </div>
                  <div className="st-row-control-auto">
                    <a
                      className="st-btn st-btn-secondary"
                      href={config.installation_url}
                      target="_blank"
                      rel="noreferrer"
                    >
                      <ExternalLink size={13} aria-hidden="true" />
                      Choose repositories on GitHub
                    </a>
                  </div>
                </div>
              )}

              {login && (
                <div className="st-divider-t p-4">
                  <div className="st-connector-code-box space-y-3">
                    <p className="st-label">Enter this code on GitHub:</p>

                    <div className="flex flex-wrap items-center justify-between gap-3">
                      <code className="select-all font-mono text-xl font-semibold tracking-[0.18em] tabular-nums">
                        {login.user_code}
                      </code>
                      <button
                        type="button"
                        onClick={() => void copyCode()}
                        className="st-btn st-btn-secondary"
                        title="Copy sign-in code"
                      >
                        {copiedCode ? (
                          <>
                            <Check size={13} aria-hidden="true" />
                            Copied
                          </>
                        ) : (
                          <>
                            <Copy size={13} aria-hidden="true" />
                            Copy
                          </>
                        )}
                      </button>
                    </div>

                    {copyCodeError && (
                      <p role="alert" className="st-desc" style={{ color: 'var(--st-danger)' }}>
                        {copyCodeError}
                      </p>
                    )}

                    <div className="flex flex-wrap items-center gap-2 pt-1">
                      <a
                        ref={continueLink}
                        className="st-btn st-btn-primary"
                        href={login.verification_uri}
                        target="_blank"
                        rel="noreferrer"
                      >
                        <ExternalLink size={13} aria-hidden="true" />
                        Continue on GitHub
                      </a>
                      <button type="button" className="st-btn st-btn-ghost" onClick={cancel}>
                        Cancel
                      </button>
                    </div>

                    <p role="status" className="st-desc flex items-center gap-1.5">
                      <Loader2 size={12} className="animate-spin" aria-hidden="true" />
                      Waiting for GitHub authorization…
                    </p>
                  </div>
                </div>
              )}
            </SettingsCard>
          )}

          {usesComposio(selectedProvider) && (
            <>
              <ComposioAccountPanel
                key={selectedProvider.id}
                providerId={selectedProvider.id}
                providerName={selectedProvider.name}
                setupNotice={selectedProvider.setup_notice}
                icon={<ProviderIcon providerId={selectedProvider.id} />}
                description={providerSummary(selectedProvider)}
                configured={composioConfigured}
                accounts={providerAccounts}
                onChanged={() => void refresh(true)}
              />
              {!composioConfigured && (
                <ComposioSetup
                  onChanged={(configured) => {
                    setComposioConfigured(configured)
                    void refresh(true)
                  }}
                />
              )}
            </>
          )}

          {selectedProvider.id === 'github' && providerAccounts.length > 0 && (
            <SettingsCard
              title="Connected accounts"
              action={<Badge>{providerAccounts.length}</Badge>}
            >
              {providerAccounts.map((account, index) => {
                const state = accountStateLabel(account.state)
                return (
                  <div key={account.id} className={index > 0 ? 'st-divider-t' : undefined}>
                    <div className="st-row">
                      <div className="min-w-0">
                        <div className="flex flex-wrap items-center gap-2">
                          <p className="st-label break-words">{account.label}</p>
                          <Badge tone={state.tone}>{state.label}</Badge>
                        </div>
                      </div>
                      <div className="st-row-control-auto">
                        <button
                          type="button"
                          className={`st-btn ${account.state === 'ready' ? 'st-btn-secondary' : 'st-btn-primary'}`}
                          disabled={busy || !!login}
                          onClick={() =>
                            account.state === 'ready'
                              ? void change(account, 'disconnect')
                              : void connect(account)
                          }
                        >
                          {account.state === 'ready' ? 'Disconnect' : 'Reconnect'}
                        </button>
                      </div>
                    </div>
                    {account.state === 'ready' && (
                      <SwitchRow
                        label={`Allow changes · ${account.label}`}
                        description="Allow issue, comment and pull request creation. Each action still requires your approval."
                        checked={selectedProvider.capabilities.some(
                          (cap) =>
                            cap.action !== 'read' && account.enabled_capabilities.includes(cap.id),
                        )}
                        disabled={busy || !!login}
                        onChange={(checked) => void change(account, 'writes', checked)}
                      />
                    )}
                  </div>
                )
              })}
            </SettingsCard>
          )}
        </div>
      ) : (
        <div className="space-y-3">
          {hasComposioProviders && (
            <ComposioSetup
              onChanged={(configured) => {
                setComposioConfigured(configured)
                void refresh(true)
              }}
            />
          )}
          <div className="st-connector-filters">
            <label className="st-connector-search">
              <Search size={13} aria-hidden="true" />
              <input
                type="search"
                className="st-input st-search"
                aria-label="Search connectors"
                placeholder="Search connectors"
                value={search}
                onChange={(event) => setSearch(event.target.value)}
              />
            </label>
            <Dropdown
              className="st-connector-category-filter"
              ariaLabel="Connector category"
              value={category}
              onChange={setCategory}
              options={[
                { value: 'all', label: 'All categories' },
                ...CATEGORIES.filter(([id]) =>
                  providers.some((provider) => providerCategory(provider) === id),
                ).map(([value, label]) => ({ value, label })),
              ]}
            />
          </div>
          {CATEGORIES.map(([id, label]) => {
            const matchingProviders = providers
              .filter(
                (provider) =>
                  providerCategory(provider) === id &&
                  (category === 'all' || category === id) &&
                  `${provider.name} ${providerSummary(provider)} ${label}`
                    .toLowerCase()
                    .includes(search.trim().toLowerCase()),
              )
              .sort((a, b) => a.name.localeCompare(b.name))
            if (matchingProviders.length === 0) return null
            return (
              <section key={id} aria-label={label} className="space-y-2">
                <h4 className="st-connector-category">
                  {label} <span>{matchingProviders.length}</span>
                </h4>
                <div className="grid gap-3 sm:grid-cols-2">
                  {matchingProviders.map((provider) => {
                    const matching = accounts.filter(
                      (account) => account.provider_id === provider.id,
                    )
                    const status =
                      provider.setup_notice &&
                      !matching.some((account) => account.state === 'ready')
                        ? {
                            label: 'Unavailable',
                            tone: 'neutral' as const,
                            dotClass: 'st-dot',
                          }
                        : providerStatus(matching)
                    const connectedLabels = matching
                      .filter((account) => account.state === 'ready')
                      .map((account) => account.label)

                    const summary = providerSummary(provider)

                    return (
                      <button
                        key={provider.id}
                        ref={(node) => {
                          cardRefs.current[provider.id] = node
                        }}
                        type="button"
                        onClick={() => openProvider(provider.id)}
                        className={`st-connector-card${provider.setup_notice ? ' st-connector-card-unavailable' : ''}`}
                      >
                        <div className="flex w-full shrink-0 items-center justify-between gap-3">
                          <span className="st-connector-icon">
                            <ProviderIcon providerId={provider.id} />
                          </span>
                          <Badge tone={status.tone}>
                            <span className={status.dotClass} aria-hidden="true" />
                            {status.label}
                          </Badge>
                        </div>

                        <div className="flex min-h-0 min-w-0 flex-1 flex-col gap-0.5">
                          <div className="flex items-center justify-between gap-2">
                            <span className="st-label truncate">{provider.name}</span>
                            <ChevronRight
                              size={14}
                              className="st-connector-chevron shrink-0"
                              aria-hidden="true"
                            />
                          </div>
                          <p className="st-desc st-connector-desc" title={summary}>
                            {summary}
                          </p>
                          {connectedLabels.length > 0 && (
                            <p className="st-hint truncate">{connectedLabels.join(', ')}</p>
                          )}
                        </div>
                      </button>
                    )
                  })}
                </div>
              </section>
            )
          })}
          {!providers.some(
            (provider) =>
              (category === 'all' || providerCategory(provider) === category) &&
              `${provider.name} ${providerSummary(provider)} ${CATEGORIES.find(([id]) => id === providerCategory(provider))?.[1]}`
                .toLowerCase()
                .includes(search.trim().toLowerCase()),
          ) && (
            <p role="status" className="st-desc py-4">
              No connectors match your search.
            </p>
          )}
        </div>
      )}
    </div>
  )
}

/** Present connector management directly from the chat command. */
export function ConnectorDialog({ onClose }: { onClose: () => void }) {
  const dialog = useRef<HTMLDivElement>(null)
  useModalFocus(dialog, { onEscape: onClose })
  const host = document.querySelector('.theme-dark, .theme-light') || document.body
  return createPortal(
    <div
      className="st-overlay fixed inset-0 below-app-bar z-50 flex items-center justify-center p-4"
      onClick={(event) => event.target === event.currentTarget && onClose()}
    >
      <div
        ref={dialog}
        role="dialog"
        aria-modal="true"
        aria-label="Connectors"
        tabIndex={-1}
        className="settings-shell flex max-h-[85vh] w-full max-w-xl flex-col overflow-hidden rounded-2xl"
      >
        <header className="st-divider-b flex items-center justify-between gap-3 px-5 py-3.5">
          <h2 className="st-page-title">Connectors</h2>
          <button
            type="button"
            className="st-btn st-btn-ghost st-btn-icon"
            aria-label="Close connectors"
            onClick={onClose}
          >
            <X size={15} />
          </button>
        </header>
        <div className="st-scroll min-h-0 flex-1 overflow-y-auto p-5">
          <ConnectorPanel />
        </div>
      </div>
    </div>,
    host,
  )
}
