import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { AgentSettings } from '../../lib/api/types'
import { FALLBACK_MODEL_OPTIONS, normalizeDraft } from './settingsConfig'
import { FOOTER_COPY } from './settingsCopy'
import { useSettingsDraft } from './useSettingsDraft'

const mocks = vi.hoisted(() => ({ updateSettings: vi.fn() }))

vi.mock('../../lib/api/settings', () => ({ updateSettings: mocks.updateSettings }))

type Server = AgentSettings['mcp']['servers'][number]

function buildSettings(overrides: Partial<AgentSettings> = {}): AgentSettings {
  return normalizeDraft({
    llm: { provider: 'openai', model_name: '', reasoning_effort: 'medium' },
    permissions: { mode: 'default', confirmations: {}, allow_delete: false },
    mcp: { enabled: true, servers: [] },
    browser: { screenshots_dir: '', allowed_domains: [] },
    tools: { skills: {} },
    identity: { agent_name: 'Monaw' },
    api_keys: { has_openai_key: false, has_google_key: false, has_tavily_key: false, has_telegram_bot_token: false },
    telegram_allowed_user_ids: '',
    telegram_allowed_chat_ids: '',
    settings_version: 'v1',
    available_skills: [],
    ...overrides,
  } as unknown as AgentSettings)
}

function server(overrides: Partial<Server> = {}): Server {
  return { server_id: 'srv-1', name: 'notes', transport: 'stdio', env: {}, headers: {}, ...overrides } as Server
}

function loaded(settings = buildSettings()) {
  const hook = renderHook(() => useSettingsDraft())
  act(() => hook.result.current.load(settings))
  return hook
}

describe('useSettingsDraft', () => {
  beforeEach(() => {
    mocks.updateSettings.mockReset()
  })

  afterEach(() => {
    cleanup()
    delete window.electronAPI
  })

  it('is dirty only while the draft differs from the saved copy', () => {
    const { result } = loaded()
    expect(result.current.isDirty).toBe(false)

    act(() => result.current.updateDraft((current) => ({ ...current, memory: { ...current.memory, enabled: false } })))
    expect(result.current.isDirty).toBe(true)

    act(() => result.current.updateDraft((current) => ({ ...current, memory: { ...current.memory, enabled: true } })))
    expect(result.current.isDirty).toBe(false)
  })

  it('ignores surrounding whitespace in the Telegram allowlists', () => {
    const { result } = loaded(buildSettings({ telegram_allowed_user_ids: '123' }))

    act(() => result.current.updateDraft((current) => ({ ...current, telegram_allowed_user_ids: ' 123 ' })))
    expect(result.current.isDirty).toBe(false)

    act(() => result.current.updateDraft((current) => ({ ...current, telegram_allowed_user_ids: '123, 456' })))
    expect(result.current.isDirty).toBe(true)
  })

  it('treats an edited connection key as unsaved and sends only edited keys outside Electron', async () => {
    const { result } = loaded()
    mocks.updateSettings.mockResolvedValue(buildSettings({ settings_version: 'v2' }))

    act(() => result.current.setSecret('tavily', 'tvly-new'))
    expect(result.current.isDirty).toBe(true)

    let outcome = ''
    await act(async () => { outcome = await result.current.save(FALLBACK_MODEL_OPTIONS) })

    expect(outcome).toBe('saved')
    const payload = mocks.updateSettings.mock.calls[0][0]
    expect(payload).toMatchObject({ expected_settings_version: 'v1', tavily_api_key: 'tvly-new' })
    expect(payload).not.toHaveProperty('openai_api_key')
    expect(result.current.isDirty).toBe(false)
  })

  it('keeps the draft and reports a failed save', async () => {
    const { result } = loaded()
    mocks.updateSettings.mockRejectedValue(new Error('Settings changed elsewhere.'))
    act(() => result.current.updateDraft((current) => ({ ...current, telegram_allowed_chat_ids: '-100' })))

    let outcome = ''
    await act(async () => { outcome = await result.current.save(FALLBACK_MODEL_OPTIONS) })

    expect(outcome).toBe('failed')
    expect(result.current.saveError).toBe(`${FOOTER_COPY.saveFailed} Settings changed elsewhere.`)
    expect(result.current.draft?.telegram_allowed_chat_ids).toBe('-100')
    expect(result.current.isDirty).toBe(true)
    expect(result.current.saving).toBe(false)
  })

  it('adopts saved settings even when storing Electron keys fails afterwards', async () => {
    const setCredential = vi.fn().mockRejectedValue(new Error('Vault locked.'))
    window.electronAPI = {
      setCredential,
      deleteCredential: vi.fn(),
      applyStoredCredentials: vi.fn(),
    } as unknown as NonNullable<typeof window.electronAPI>
    const { result } = loaded()
    mocks.updateSettings.mockResolvedValue(buildSettings({ settings_version: 'v2', telegram_allowed_chat_ids: '-100' }))
    act(() => {
      result.current.updateDraft((current) => ({ ...current, telegram_allowed_chat_ids: '-100' }))
      result.current.setSecret('openai', 'sk-new')
    })

    let outcome = ''
    await act(async () => { outcome = await result.current.save(FALLBACK_MODEL_OPTIONS) })

    expect(outcome).toBe('keys_failed')
    expect(mocks.updateSettings.mock.calls[0][0]).not.toHaveProperty('openai_api_key')
    expect(setCredential).toHaveBeenCalledWith('openai', 'sk-new')
    expect(result.current.saveError).toBe(`${FOOTER_COPY.keysFailed} Vault locked.`)
    expect(result.current.draft?.settings_version).toBe('v2')
    expect(result.current.isDirty).toBe(true)

    // The retry carries the new version, so it is not rejected as stale.
    setCredential.mockResolvedValue({})
    vi.mocked(window.electronAPI!.applyStoredCredentials).mockResolvedValue({
      openai: true, google: false, tavily: false, telegramBot: false,
    })
    mocks.updateSettings.mockResolvedValue(buildSettings({ settings_version: 'v3', telegram_allowed_chat_ids: '-100' }))
    await act(async () => { outcome = await result.current.save(FALLBACK_MODEL_OPTIONS) })

    expect(outcome).toBe('saved')
    expect(mocks.updateSettings.mock.calls[1][0]).toMatchObject({ expected_settings_version: 'v2' })
    expect(result.current.draft?.api_keys.has_openai_key).toBe(true)
    expect(result.current.isDirty).toBe(false)
  })

  it('reconciles an immediate MCP credential change without discarding other edits', () => {
    const { result } = loaded(buildSettings({ mcp: { enabled: true, servers: [server({ env: { TOKEN: 'plain', MODE: 'fast' } })] } }))
    act(() => result.current.updateDraft((current) => ({ ...current, memory: { ...current.memory, enabled: false } })))

    const fresh = buildSettings({
      settings_version: 'v2',
      mcp: {
        enabled: true,
        servers: [server({ env: { MODE: 'fast' }, credential_refs: { env: { TOKEN: 'ref-1' }, headers: {} } } as Partial<Server>)],
      },
    })
    act(() => result.current.reconcileMcpCredentials(fresh))

    const draftServer = result.current.draft!.mcp.servers[0]
    expect(draftServer.env).toEqual({ MODE: 'fast' })
    expect(draftServer.credential_refs).toEqual({ env: { TOKEN: 'ref-1' }, headers: {} })
    expect(result.current.draft?.settings_version).toBe('v2')
    expect(result.current.draft?.memory.enabled).toBe(false)

    act(() => result.current.updateDraft((current) => ({ ...current, memory: { ...current.memory, enabled: true } })))
    expect(result.current.isDirty).toBe(false)
  })
})
