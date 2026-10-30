import { describe, expect, it } from 'vitest'

import type { AgentSettings, BrowserUseDiagnostics, DiagnosticsSummary } from '../../lib/api/types'
import { emptySandbox } from '../settings/settingsConfig'
import { optionalSteps, requiredSteps } from './readiness'

function settings(overrides: Record<string, unknown> = {}): AgentSettings {
  return {
    llm: { provider: 'openai', model_name: 'gpt-6-luna', reasoning_effort: 'medium' },
    api_keys: { has_openai_key: false, has_google_key: false, has_tavily_key: false, has_telegram_bot_token: false },
    permissions: { mode: 'default' },
    tools: { skills: {} },
    sandbox: emptySandbox(),
    mcp: { enabled: true, servers: [] },
    ...overrides,
  } as unknown as AgentSettings
}

function summary(overrides: Partial<DiagnosticsSummary> = {}): DiagnosticsSummary {
  return {
    telegram: { configured: false, running: false, startup_error: '' },
    mcp: { enabled: true, configured_servers: 0, runtime_servers: 0, connected_servers: 0, unhealthy_servers: 0 },
    sandbox: { backends: {} },
    ...overrides,
  } as unknown as DiagnosticsSummary
}

const model = (steps: ReturnType<typeof requiredSteps>) => steps.find((step) => step.id === 'model')!

describe('required steps', () => {
  it('checks the credential the selected provider actually needs', () => {
    expect(model(requiredSteps({ settings: settings(), account: null, workspaceDir: '' })).state).toBe('action')
    expect(model(requiredSteps({
      settings: settings({ api_keys: { has_openai_key: true, has_google_key: false } }), account: null, workspaceDir: '',
    })).state).toBe('ready')
    // A Google key does not make the OpenAI provider ready.
    expect(model(requiredSteps({
      settings: settings({ api_keys: { has_openai_key: false, has_google_key: true } }), account: null, workspaceDir: '',
    })).state).toBe('action')
    expect(model(requiredSteps({
      settings: settings({ llm: { provider: 'gemini', model_name: 'gemini-x' }, api_keys: { has_google_key: true } }), account: null, workspaceDir: '',
    })).state).toBe('ready')
  })

  it('waits for the account check before judging the account provider', () => {
    const codex = settings({ llm: { provider: 'codex', model_name: 'gpt-6-luna' } })
    expect(model(requiredSteps({ settings: codex, account: null, workspaceDir: '' })).state).toBe('checking')
    expect(model(requiredSteps({
      settings: codex, account: { connected: false, plan: '', limits: [], usage_error: '' }, workspaceDir: '',
    })).state).toBe('action')
    expect(model(requiredSteps({
      settings: codex, account: { connected: true, plan: 'plus', limits: [], usage_error: '' }, workspaceDir: '',
    })).state).toBe('ready')
  })

  it('describes the workspace and the current permission mode', () => {
    const steps = requiredSteps({ settings: settings({ permissions: { mode: 'full_access' } }), account: null, workspaceDir: 'C:/work/app' })
    expect(steps.find((step) => step.id === 'workspace')?.detail).toBe('Working in C:/work/app.')
    expect(steps.find((step) => step.id === 'permissions')?.title).toBe('Permissions: Full Access')
  })
})

describe('optional capabilities', () => {
  it('lists nothing the user has not turned on', () => {
    expect(optionalSteps({ settings: settings(), summary: summary(), browser: null })).toEqual([])
  })

  it('reports browser control from the extension connection, not the toggle', () => {
    const on = settings({ tools: { skills: { 'browser-use': true } } })
    const browser = (extension: Partial<BrowserUseDiagnostics['extension']>, chromeFound = true) => (
      { chrome_found: chromeFound, extension } as unknown as BrowserUseDiagnostics
    )
    const state = (diagnostics: BrowserUseDiagnostics | null) => optionalSteps({ settings: on, summary: null, browser: diagnostics })[0].state

    expect(state(null)).toBe('checking')
    expect(state(browser({}, false))).toBe('problem')
    expect(state(browser({ installed: false }))).toBe('action')
    expect(state(browser({ installed: true, connected: false }))).toBe('action')
    expect(state(browser({ installed: true, connected: true }))).toBe('ready')
  })

  it('reports Docker only when commands must run in a container, from the sandbox check', () => {
    const docker = (mode: string) => settings({ sandbox: { ...emptySandbox(), enabled: true, mode, docker: { ...emptySandbox().docker, enabled: true } } })
    const unavailable = summary({ sandbox: { backends: { docker: { available: false, reason: 'Docker Desktop is not running.' } } } as never })

    const [enforced] = optionalSteps({ settings: docker('enforce'), summary: unavailable, browser: null })
    expect(enforced.state).toBe('problem')
    expect(enforced.detail).toBe('Docker Desktop is not running. Commands are blocked until it is.')
    expect(optionalSteps({ settings: docker('auto'), summary: unavailable, browser: null })).toEqual([])
    expect(optionalSteps({
      settings: docker('docker'), summary: summary({ sandbox: { backends: { docker: { available: true } } } as never }), browser: null,
    })[0].state).toBe('ready')
  })

  it('reports Telegram and MCP from their running state', () => {
    const configured = settings({
      api_keys: { has_telegram_bot_token: true },
      mcp: { enabled: true, servers: [{ enabled: true }, { enabled: true }, { enabled: false }] },
    })
    const steps = optionalSteps({
      settings: configured,
      summary: summary({
        telegram: { configured: true, running: true, startup_error: '' },
        mcp: { enabled: true, configured_servers: 3, runtime_servers: 2, connected_servers: 1, unhealthy_servers: 1 },
      }),
      browser: null,
    })

    expect(steps.map((step) => [step.id, step.state])).toEqual([['telegram', 'ready'], ['mcp', 'problem']])
    expect(steps[1].detail).toBe('1 of 2 connected, 1 unhealthy.')
  })
})
