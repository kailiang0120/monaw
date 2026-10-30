import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  fetchModelOptions,
  fetchOpenAIAccountStatus,
  fetchSettings,
  fetchWorkspaceInstructions,
  resetWorkspaceInstructions,
  updateSettings,
  updateWorkspaceInstructions,
  startOpenAIAccountLogin,
} from '../lib/api/settings'
import { fetchBrowserDiagnostics, installBrowserExtension, reconnectBrowser } from '../lib/api/diagnostics'
import { SettingsModal } from '../features/settings/SettingsModal'

const mocks = vi.hoisted(() => ({
  fetchSettings: vi.fn(),
  fetchModelOptions: vi.fn(),
  fetchOpenAIAccountStatus: vi.fn(),
  startOpenAIAccountLogin: vi.fn(),
  logoutOpenAIAccount: vi.fn(),
  fetchSandboxStatus: vi.fn(),
  fetchSpeechToTextStatus: vi.fn(),
  fetchWorkspaceInstructions: vi.fn(),
  downloadSpeechToTextModel: vi.fn(),
  offloadSpeechToTextModel: vi.fn(),
  deleteSpeechToTextModel: vi.fn(),
  updateWorkspaceInstructions: vi.fn(),
  resetWorkspaceInstructions: vi.fn(),
  updateSettings: vi.fn(),
}))

vi.mock('../lib/api/diagnostics', () => {
  const browserStatus = {
    skill_enabled: true,
    skill_available: true,
    skill_unavailable_reason: '',
    chrome_found: true,
    chrome_executable: '',
    devtools: {
      server: 'chrome-devtools',
      running: true,
      connected: true,
      state: 'connected',
      startup_phase: 'ready',
      tool_count: 26,
      last_error: '',
      unhealthy_reason: '',
      connected_at: null,
    },
    extension: {
      installed: true, paired: true, installed_version: '1.0.0', bundled_version: '1.0.0', update_available: false,
      server_running: true, connected: true, connected_version: '1.0.0', last_error: '',
    },
    last_error: '',
  }
  return {
    fetchBrowserDiagnostics: vi.fn().mockResolvedValue(browserStatus),
    fetchMCPDiagnostics: vi.fn().mockResolvedValue([]),
    reconnectBrowser: vi.fn().mockResolvedValue(browserStatus),
    installBrowserExtension: vi.fn(),
    reconnectMCPServer: vi.fn(),
  }
})

vi.mock('../lib/api/settings', () => ({
  fetchSettings: mocks.fetchSettings,
  fetchModelOptions: mocks.fetchModelOptions,
  fetchOpenAIAccountStatus: mocks.fetchOpenAIAccountStatus,
  startOpenAIAccountLogin: mocks.startOpenAIAccountLogin,
  logoutOpenAIAccount: mocks.logoutOpenAIAccount,
  fetchSandboxStatus: mocks.fetchSandboxStatus,
  fetchSpeechToTextStatus: mocks.fetchSpeechToTextStatus,
  fetchWorkspaceInstructions: mocks.fetchWorkspaceInstructions,
  downloadSpeechToTextModel: mocks.downloadSpeechToTextModel,
  offloadSpeechToTextModel: mocks.offloadSpeechToTextModel,
  deleteSpeechToTextModel: mocks.deleteSpeechToTextModel,
  updateWorkspaceInstructions: mocks.updateWorkspaceInstructions,
  resetWorkspaceInstructions: mocks.resetWorkspaceInstructions,
  updateSettings: mocks.updateSettings,
}))

function buildSettings(overrides: Record<string, unknown> = {}) {
  return {
    llm: {
      provider: 'openai',
      model_name: 'gpt-5.6-luna',
      reasoning_effort: 'medium',
      max_iterations_per_turn: 40,
      max_turn_seconds: 1800,
      max_llm_call_seconds: 300,
    },
    speech_to_text: {
      engine: 'local',
      local_model: 'base',
      cloud_provider: 'gemini',
      cloud_model: 'gemini-2.5-flash',
    },
    mcp: {
      enabled: true,
      servers: [],
    },
    browser: {
      screenshots_dir: 'C:/runtime/browser/screenshots',
      allowed_domains: [],
    },
    memory: {
      enabled: true,
      auto_learn: true,
      retrieval_limit: 6,
      max_injected_chars: 2500,
      min_relevance_score: 0.15,
    },
    tools: {
      skills: {
        'notion-sync': true,
        'browser-use': true,
        core: true,
        exec: true,
        'computer-use': true,
        filesystem: true,
        memory: true,
        'mcp-bridge': false,
      },
    },
    permissions: {
      mode: 'default',
      confirmations: {
        mutate: true,
        delete: true,
        launch_app: true,
        click: true,
        type: true,
      },
      blocked_roots: [],
      path_rules: [],
      app_rules: [],
      allow_delete: false,
      dangerous_actions_require_confirm: true,
      allow_screen_fallback: false,
      custom_profile: {
        confirmations: {
          mutate: true,
          delete: true,
          launch_app: true,
          click: true,
          type: true,
        },
        blocked_roots: [],
        path_rules: [],
        app_rules: [],
        allow_delete: false,
        dangerous_actions_require_confirm: true,
        allow_screen_fallback: false,
      },
    },
    identity: {
      agent_name: 'Monaw',
      user_name: '',
      user_identity: '',
      communication_style: '',
    },
    settings_version: 'settings-v1',
    available_skills: [
      {
        slug: 'notion_sync',
        name: 'notion-sync',
        display_name: 'Notion sync',
        summary: 'Sync notes with Notion.',
        description: 'User-added skill',
        version: '1.0.0',
        enabled_by_default: true,
        enabled: true,
        available: true,
        always: false,
        unavailable_reason: '',
        load_error: '',
        tier: 'recommended',
        recommended: true,
      },
      {
        slug: 'browser_use',
        name: 'browser-use',
        display_name: 'Browser automation',
        summary: 'Browse and interact with websites.',
        description: 'Browser automation',
        version: '1.0.0',
        enabled_by_default: true,
        enabled: true,
        available: true,
        always: true,
        unavailable_reason: '',
        load_error: '',
        tier: 'internal',
        recommended: true,
      },
      {
        slug: 'core',
        name: 'core',
        display_name: 'Core utilities',
        summary: 'Calculator and system controls.',
        description: 'Core tools',
        version: '1.0.0',
        enabled_by_default: true,
        enabled: true,
        available: true,
        always: true,
        unavailable_reason: '',
        load_error: '',
        tier: 'internal',
        recommended: false,
      },
      {
        slug: 'exec',
        name: 'exec',
        display_name: 'Command execution',
        summary: 'Run local commands.',
        description: 'Exec tools',
        version: '1.0.0',
        enabled_by_default: true,
        enabled: true,
        available: true,
        always: true,
        unavailable_reason: '',
        load_error: '',
        tier: 'internal',
        recommended: false,
      },
      {
        slug: 'computer_use',
        name: 'computer-use',
        display_name: 'Windows computer use',
        summary: 'Control native Windows applications.',
        description: 'Computer use',
        version: '1.0.0',
        enabled_by_default: true,
        enabled: true,
        available: true,
        always: true,
        unavailable_reason: '',
        load_error: '',
        tier: 'internal',
        recommended: true,
      },
      {
        slug: 'filesystem',
        name: 'filesystem',
        display_name: 'File management',
        summary: 'Manage local files.',
        description: 'Filesystem tools',
        version: '1.0.0',
        enabled_by_default: true,
        enabled: true,
        available: true,
        always: true,
        unavailable_reason: '',
        load_error: '',
        tier: 'internal',
        recommended: false,
      },
      {
        slug: 'memory',
        name: 'memory',
        display_name: 'Long-term memory',
        summary: 'Preserve durable context.',
        description: 'Long-term memory',
        version: '1.0.0',
        enabled_by_default: true,
        enabled: true,
        available: true,
        always: true,
        unavailable_reason: '',
        load_error: '',
        tier: 'internal',
        recommended: false,
      },
      {
        slug: 'mcp_bridge',
        name: 'mcp-bridge',
        display_name: 'MCP integrations',
        summary: 'Connect MCP servers.',
        description: 'MCP bridge',
        version: '1.0.0',
        enabled_by_default: false,
        enabled: false,
        available: true,
        always: true,
        unavailable_reason: '',
        load_error: '',
        tier: 'internal',
        recommended: false,
      },
    ],
    api_keys: {
      has_openai_key: true,
      has_google_key: true,
      has_tavily_key: true,
      has_telegram_bot_token: true,
      has_telegram_allowlist: false,
    },
    telegram_allowed_user_ids: '',
    telegram_allowed_chat_ids: '',
    ...overrides,
  }
}

describe('SettingsModal', () => {
  beforeEach(() => {
    vi.mocked(fetchOpenAIAccountStatus).mockResolvedValue({ connected: false, plan: '', limits: [], usage_error: '' })
    vi.mocked(fetchSettings).mockResolvedValue(buildSettings() as any)
    vi.mocked(fetchModelOptions).mockResolvedValue({
      providers: [
        { id: 'openai', label: 'OpenAI', models: ['gpt-6-luna', 'gpt-6.1-sol', 'gpt-6-astra'] },
        {
          id: 'gemini',
          label: 'Google',
          models: ['gemini-pro-latest', 'gemini-flash-latest', 'gemini-flash-lite-latest'],
        },
      ],
    } as any)
    mocks.fetchSandboxStatus.mockResolvedValue({
      enabled: true,
      mode: 'auto',
      default_profile: 'standard',
      default_network: 'deny',
      default_write_strategy: 'copy_out',
      backends: {},
    })
    mocks.fetchSpeechToTextStatus.mockResolvedValue({
      provider: 'faster-whisper',
      label: 'Powered by faster-whisper',
      engine: 'local',
      cloud_provider: 'gemini',
      cloud_model: 'gemini-2.5-flash',
      cloud_configured: true,
      model_id: 'base',
      model_label: 'Whisper base',
      model_size: '~142 MB',
      downloaded: false,
      download_dir: 'C:/runtime/models/faster-whisper/base',
      dependency_available: true,
      loaded: false,
    })
    mocks.downloadSpeechToTextModel.mockResolvedValue({
      provider: 'faster-whisper',
      label: 'Powered by faster-whisper',
      engine: 'local',
      cloud_provider: 'gemini',
      cloud_model: 'gemini-2.5-flash',
      cloud_configured: true,
      model_id: 'base',
      model_label: 'Whisper base',
      model_size: '~142 MB',
      downloaded: true,
      download_dir: 'C:/runtime/models/faster-whisper/base',
      dependency_available: true,
      loaded: false,
    })
    mocks.offloadSpeechToTextModel.mockResolvedValue({
      provider: 'faster-whisper',
      label: 'Powered by faster-whisper',
      engine: 'local',
      cloud_provider: 'gemini',
      cloud_model: 'gemini-2.5-flash',
      cloud_configured: true,
      model_id: 'base',
      model_label: 'Whisper base',
      model_size: '~142 MB',
      downloaded: true,
      download_dir: 'C:/runtime/models/faster-whisper/base',
      dependency_available: true,
      loaded: false,
    })
    mocks.deleteSpeechToTextModel.mockResolvedValue({
      provider: 'faster-whisper',
      label: 'Powered by faster-whisper',
      engine: 'local',
      cloud_provider: 'gemini',
      cloud_model: 'gemini-2.5-flash',
      cloud_configured: true,
      model_id: 'base',
      model_label: 'Whisper base',
      model_size: '~142 MB',
      downloaded: false,
      download_dir: 'C:/runtime/models/faster-whisper/base',
      dependency_available: true,
      loaded: false,
    })
    mocks.fetchWorkspaceInstructions.mockResolvedValue({
      path: 'C:/Users/Kai/.monaw/workspace/AGENTS.md',
      content: '# AGENTS.md\n\n- Keep changes focused.\n',
    })
    mocks.updateWorkspaceInstructions.mockImplementation(async (content: string) => ({
      path: 'C:/Users/Kai/.monaw/workspace/AGENTS.md',
      content,
    }))
    mocks.resetWorkspaceInstructions.mockResolvedValue({
      path: 'C:/Users/Kai/.monaw/workspace/AGENTS.md',
      content: '# AGENTS.md\n\n- Ignore this file if the task is not coding.\n',
    })
    vi.mocked(updateSettings).mockResolvedValue(buildSettings() as any)
    delete (window as any).electronAPI
  })

  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
  })

  it('keeps model selection and image help out of Settings', async () => {
    render(<SettingsModal onClose={() => {}} />)

    expect(await screen.findByRole('heading', { name: 'Voice' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Provider' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Reasoning effort' })).not.toBeInTheDocument()
    expect(screen.queryByText('Reading images and screenshots')).not.toBeInTheDocument()
  })

  it('shows Chrome status and only the extension-era browser settings', async () => {
    render(<SettingsModal onClose={() => {}} />)

    fireEvent.click(await screen.findByRole('button', { name: 'Browser' }))
    expect(await screen.findByText('Found')).toBeInTheDocument()
    expect(screen.getAllByText('Monaw extension').length).toBeGreaterThan(0)
    expect(screen.getByText('Connected')).toBeInTheDocument()
    // Chrome remote debugging was removed: no connection choice, profile picker or Allow-prompt switch.
    expect(screen.queryByText('DevTools connection')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Chrome remote debugging' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Chrome profile' })).not.toBeInTheDocument()
    expect(screen.queryByRole('checkbox', { name: "Auto-approve Chrome's connection prompt" })).not.toBeInTheDocument()
    expect(screen.queryByText(/remote debugging/i)).not.toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: 'Navigation target domains' })).toBeInTheDocument()
    expect(screen.getByText(/Does not restrict existing tabs, snapshots, screenshots, clicks, popups, or Chrome redirects/)).toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: 'Screenshots folder' })).toHaveValue('C:/runtime/browser/screenshots')
    expect(screen.getByRole('button', { name: 'Reconnect' })).toBeInTheDocument()
    for (const removed of [
      'Hide browser window',
      'Keep browser open between requests',
      'Advanced page reading',
      'Browser trace folder',
      'Downloads folder',
      'Diagnostics',
    ]) {
      expect(screen.queryByText(removed)).not.toBeInTheDocument()
    }
    expect(screen.queryByText(/browser-use package/)).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Browser mode' })).not.toBeInTheDocument()
    expect(screen.queryByLabelText('CDP endpoint')).not.toBeInTheDocument()
  })

  it('saves browser settings without removed browser keys', async () => {
    mocks.updateSettings.mockClear()
    render(<SettingsModal onClose={() => {}} />)

    fireEvent.click(await screen.findByRole('button', { name: 'Browser' }))
    await screen.findByText('Found')
    fireEvent.change(screen.getByRole('textbox', { name: 'Navigation target domains' }), {
      target: { value: 'example.com, docs.example.com' },
    })
    fireEvent.click(screen.getByRole('button', { name: /Save/i }))

    await waitFor(() => {
      expect(updateSettings).toHaveBeenCalledWith(expect.objectContaining({
        browser: expect.objectContaining({ allowed_domains: ['example.com', 'docs.example.com'] }),
      }))
    })
    const calls = vi.mocked(updateSettings).mock.calls
    const savedBrowser = calls[calls.length - 1]?.[0]?.browser ?? {}
    for (const removed of [
      'headless', 'keep_alive', 'dom_inspection_engine', 'downloads_dir', 'traces_dir', 'mode',
      'connection', 'system_profile_directory', 'auto_approve_local_debugging',
    ]) {
      expect(savedBrowser).not.toHaveProperty(removed)
    }
  })

  it('reconnects the browser server from the Browser page', async () => {
    render(<SettingsModal onClose={() => {}} />)

    fireEvent.click(await screen.findByRole('button', { name: 'Browser' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Reconnect' }))

    await waitFor(() => expect(reconnectBrowser).toHaveBeenCalledTimes(1))
  })

  it('sets up the Monaw extension and shows the Load unpacked steps', async () => {
    const base = await fetchBrowserDiagnostics()
    const extension = {
      installed: false, paired: false, installed_version: '', bundled_version: '1.0.0', update_available: false,
      server_running: false, connected: false, connected_version: '', last_error: '',
    }
    vi.mocked(fetchBrowserDiagnostics).mockResolvedValue({ ...base, extension })
    const installedStatus = {
      ...base,
      extension: { ...extension, installed: true, paired: true, installed_version: '1.0.0', server_running: true },
    }
    vi.mocked(installBrowserExtension).mockResolvedValue({
      folder: 'C:/Users/me/.monaw/browser/extension',
      version: '1.0.0',
      folder_opened: true,
      extensions_page_opened: false,
      status: installedStatus,
    })
    render(<SettingsModal onClose={() => {}} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Browser' }))
    await screen.findByText('Found')
    expect(screen.getByText('Not set up')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Set up extension' }))
    expect(await screen.findByRole('textbox', { name: 'Extension folder' })).toHaveValue('C:/Users/me/.monaw/browser/extension')
    expect(screen.getByText('Click Load unpacked.')).toBeInTheDocument()
    expect(screen.getByText(/Monaw could not open it/)).toBeInTheDocument()
    expect(screen.getByText('Waiting for Chrome')).toBeInTheDocument()
    vi.mocked(fetchBrowserDiagnostics).mockResolvedValue(base)
  })

  it.each(['desktop', 'browser', 'failure'] as const)('copies the extension folder with %s clipboard behavior', async (mode) => {
    const base = await fetchBrowserDiagnostics()
    const status = { ...base,
      extension: { installed: true, paired: true, installed_version: '1.0.0', bundled_version: '1.0.0',
        update_available: false, server_running: true, connected: false, connected_version: '', last_error: '' } }
    vi.mocked(fetchBrowserDiagnostics).mockResolvedValueOnce(status)
    vi.mocked(installBrowserExtension).mockResolvedValue({ folder: 'C:/Monaw Test/浏览器/extension', version: '1.0.0',
      folder_opened: true, extensions_page_opened: true, status })
    const browserWrite = vi.fn().mockResolvedValue(undefined)
    vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText: browserWrite } })
    const desktopWrite = vi.fn().mockResolvedValue(undefined)
    if (mode !== 'browser') {
      // Electron denies navigator.clipboard. Copy must use the preload bridge.
      browserWrite.mockRejectedValue(new Error('Clipboard permission denied'))
      Object.defineProperty(window, 'electronAPI', { configurable: true, value: { writeClipboardText: desktopWrite } })
    }
    if (mode === 'failure') desktopWrite.mockRejectedValue(new Error('Fixture OS clipboard failure'))
    render(<SettingsModal onClose={() => {}} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Browser' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Set up again' }))
    await screen.findByRole('textbox', { name: 'Extension folder' })
    fireEvent.click(screen.getByRole('button', { name: 'Copy' }))
    if (mode === 'failure') {
      expect(await screen.findByRole('alert')).toHaveTextContent('Select the folder and press Ctrl+C')
      expect(screen.queryByRole('button', { name: 'Copied' })).not.toBeInTheDocument()
      expect(screen.getByRole('textbox', { name: 'Extension folder' })).toHaveValue('C:/Monaw Test/浏览器/extension')
    } else {
      expect(await screen.findByRole('button', { name: 'Copied' })).toBeInTheDocument()
      expect(mode === 'desktop' ? desktopWrite : browserWrite).toHaveBeenCalledWith('C:/Monaw Test/浏览器/extension')
    }
    if (mode !== 'browser') expect(browserWrite).not.toHaveBeenCalled()
  })

  it('shows the extension as connected', async () => {
    const base = await fetchBrowserDiagnostics()
    vi.mocked(fetchBrowserDiagnostics).mockResolvedValueOnce(base)
    render(<SettingsModal onClose={() => {}} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Browser' }))
    expect(await screen.findByText('Connected')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Set up again' })).toBeInTheDocument()
    expect(screen.getByText(/Connected \(version 1\.0\.0\)/)).toBeInTheDocument()
  })

  it('warns when Chrome is not installed', async () => {
    vi.mocked(fetchBrowserDiagnostics).mockResolvedValueOnce({
      ...(await fetchBrowserDiagnostics()),
      chrome_found: false,
    })
    render(<SettingsModal onClose={() => {}} />)

    fireEvent.click(await screen.findByRole('button', { name: 'Browser' }))
    expect(await screen.findByText('Not found')).toBeInTheDocument()
    expect(screen.getByText(/Google Chrome was not found/)).toBeInTheDocument()
  })

  it('lists only user-added skills and MCP servers in Tools and preserves edits when switching', async () => {
    mocks.updateSettings.mockClear()
    render(<SettingsModal onClose={() => {}} />)

    fireEvent.click(await screen.findByRole('button', { name: 'Tools' }))

    expect(screen.getAllByRole('tab').map((tab) => tab.textContent)).toEqual(['Skills', 'MCP', 'Plugins'])
    expect(screen.getByRole('tab', { name: 'Skills' })).toHaveAttribute('aria-selected', 'true')
    expect(await screen.findByText('Notion sync')).toBeInTheDocument()
    expect(screen.queryByText('Browser automation')).not.toBeInTheDocument()
    expect(screen.queryByText('Core utilities')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Use skill defaults' })).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('checkbox', { name: 'Notion sync' }))
    fireEvent.keyDown(screen.getByRole('tab', { name: 'Skills' }), { key: 'ArrowRight' })
    expect(screen.getByRole('tab', { name: 'MCP' })).toHaveFocus()
    expect(screen.getByRole('button', { name: 'Server template' })).toHaveTextContent('Custom stdio')
    fireEvent.click(screen.getByRole('button', { name: 'Server template' }))
    expect(screen.queryByRole('option', { name: /Chrome DevTools/ })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('option', { name: 'Custom stdio' }))
    fireEvent.click(screen.getByRole('button', { name: 'Add server' }))
    fireEvent.change(screen.getByRole('textbox', { name: 'Server name 1' }), { target: { value: 'my-files' } })

    fireEvent.click(screen.getByRole('tab', { name: 'Plugins' }))
    expect(screen.getByText('Plugin installation is not available yet')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Manage MCP connections' }))
    expect(screen.getByRole('textbox', { name: 'Server name 1' })).toHaveValue('my-files')
    fireEvent.click(screen.getByRole('tab', { name: 'Skills' }))
    expect(screen.getByRole('checkbox', { name: 'Notion sync' })).not.toBeChecked()
    expect(updateSettings).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: /Save/i }))
    await waitFor(() => expect(updateSettings).toHaveBeenCalledWith(expect.objectContaining({
      tools: expect.objectContaining({ skills: expect.objectContaining({ 'notion-sync': false }) }),
      mcp: expect.objectContaining({ servers: [expect.objectContaining({ name: 'my-files' })] }),
    })))
  })

  it('shows an empty state when the user has not added skills', async () => {
    const settings = buildSettings() as any
    settings.available_skills = settings.available_skills.filter((skill: any) => skill.tier === 'internal')
    vi.mocked(fetchSettings).mockResolvedValue(settings)
    render(<SettingsModal onClose={() => {}} />)

    fireEvent.click(await screen.findByRole('button', { name: 'Tools' }))

    expect(await screen.findByText('No skills added yet. Ask Monaw to create one, and it will appear here.')).toBeInTheDocument()
  })

  it('shows faster-whisper speech-to-text status and downloads the local model', async () => {
    render(<SettingsModal onClose={() => {}} />)

    expect(await screen.findByText('Whisper base (~142 MB)')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /Download local speech model/i }))

    await waitFor(() => {
      expect(mocks.downloadSpeechToTextModel).toHaveBeenCalled()
    })
    expect(await screen.findByRole('button', { name: /Download local speech model/i })).toBeDisabled()
  })

  it('uses masked Electron credential status without reading plaintext keys', async () => {
    vi.mocked(fetchSettings).mockResolvedValue(buildSettings({
      api_keys: {
        has_openai_key: false,
        has_google_key: false,
        has_tavily_key: false,
        has_telegram_bot_token: false,
      },
    }) as any)
    ;(window as any).electronAPI = {
      isElectron: true,
      applyStoredCredentials: vi.fn().mockResolvedValue({
        openai: true,
        google: true,
        tavily: true,
        telegramBot: true,
      }),
      credentialStatus: vi.fn(),
      setCredential: vi.fn(),
      deleteCredential: vi.fn(),
    }

    render(<SettingsModal onClose={() => {}} />)

    fireEvent.click(await screen.findByRole('button', { name: /Connections/i }))
    expect(await screen.findByText(/OpenAI key saved: yes/i)).toBeInTheDocument()
    expect(screen.getAllByText(/Google key saved: yes/i).length).toBeGreaterThan(0)
    fireEvent.click(screen.getByRole('button', { name: 'Portal' }))
    fireEvent.click(screen.getByRole('option', { name: 'Telegram' }))
    expect(screen.getByText(/Telegram token saved: yes/i)).toBeInTheDocument()
    expect(updateSettings).not.toHaveBeenCalledWith(expect.objectContaining({
      openai_api_key: expect.anything(),
    }))
  })

  it('offers ChatGPT account sign-in alongside the API key connection', async () => {
    vi.mocked(startOpenAIAccountLogin).mockResolvedValue({
      auth_url: 'https://auth.openai.com/example',
      login_id: 'login-1',
    })
    const openSpy = vi.spyOn(window, 'open').mockImplementation(() => null)
    render(<SettingsModal onClose={() => {}} />)
    fireEvent.click(await screen.findByRole('button', { name: /Connections/i }))
    expect(await screen.findByText('OpenAI account')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Sign in with ChatGPT' }))
    await waitFor(() => expect(startOpenAIAccountLogin).toHaveBeenCalledOnce())
    expect(openSpy).toHaveBeenCalledWith('https://auth.openai.com/example', '_blank', 'noopener,noreferrer')
    expect(screen.getByText('Open sign-in page')).toHaveAttribute('href', 'https://auth.openai.com/example')
    openSpy.mockRestore()
  })

  it('shows account usage remaining and reset times when connected', async () => {
    vi.mocked(fetchOpenAIAccountStatus).mockResolvedValue({
      connected: true,
      plan: 'plus',
      usage_error: '',
      limits: [
        {
          limit_id: 'codex', limit_name: 'Codex', window: 'primary',
          used_percent: 20, window_duration_mins: 300, resets_at: 1800000000,
        },
        {
          limit_id: 'codex', limit_name: 'Codex', window: 'secondary',
          used_percent: 60, window_duration_mins: 10080, resets_at: 1800500000,
        },
      ],
    })
    render(<SettingsModal onClose={() => {}} />)
    fireEvent.click(await screen.findByRole('button', { name: /Connections/i }))
    expect(await screen.findByText('5-hour limit')).toBeInTheDocument()
    expect(screen.getByText('Weekly limit')).toBeInTheDocument()
    expect(screen.getByText('80% left')).toBeInTheDocument()
    expect(screen.getByText('40% left')).toBeInTheDocument()
    expect(screen.getAllByText(/^Resets /)).toHaveLength(2)
    const callsBeforeRefresh = vi.mocked(fetchOpenAIAccountStatus).mock.calls.length
    fireEvent.click(screen.getByRole('button', { name: 'Refresh usage' }))
    await waitFor(() => expect(fetchOpenAIAccountStatus).toHaveBeenCalledTimes(callsBeforeRefresh + 1))
  })

  it('saves Telegram allowlist fields from the Telegram connection portal', async () => {
    ;(window as any).electronAPI = {
      isElectron: true,
      applyStoredCredentials: vi.fn().mockResolvedValue({
        openai: false,
        google: false,
        tavily: false,
        telegramBot: false,
      }),
      credentialStatus: vi.fn(),
      setCredential: vi.fn(),
      deleteCredential: vi.fn(),
    }

    render(<SettingsModal onClose={() => {}} />)

    fireEvent.click(await screen.findByRole('button', { name: /Connections/i }))
    fireEvent.click(screen.getByRole('button', { name: 'Portal' }))
    fireEvent.click(screen.getByRole('option', { name: 'Telegram' }))
    fireEvent.change(await screen.findByLabelText('Allowed user IDs'), {
      target: { value: '123456789' },
    })
    fireEvent.change(screen.getByLabelText('Allowed chat IDs'), {
      target: { value: '-1001234567890' },
    })
    fireEvent.click(screen.getByRole('button', { name: /Save/i }))

    await waitFor(() => {
      expect(updateSettings).toHaveBeenCalledWith(expect.objectContaining({
        telegram_allowed_user_ids: '123456789',
        telegram_allowed_chat_ids: '-1001234567890',
      }))
    })
  })

  it('does not delete saved Electron keys when secret fields were not edited', async () => {
    vi.mocked(fetchSettings).mockResolvedValue(buildSettings({
      api_keys: {
        has_openai_key: true,
        has_google_key: true,
        has_tavily_key: true,
        has_telegram_bot_token: true,
      },
    }) as any)
    const deleteCredential = vi.fn()
    ;(window as any).electronAPI = {
      isElectron: true,
      applyStoredCredentials: vi.fn().mockResolvedValue({
        openai: true,
        google: true,
        tavily: true,
        telegramBot: true,
      }),
      credentialStatus: vi.fn(),
      setCredential: vi.fn(),
      deleteCredential,
    }

    render(<SettingsModal onClose={() => {}} />)

    fireEvent.click((await screen.findAllByRole('button', { name: /Identity/i }))[0])
    fireEvent.change(await screen.findByLabelText('Your name'), { target: { value: 'Kai' } })
    fireEvent.click(screen.getByRole('button', { name: /Save/i }))

    await waitFor(() => {
      expect(updateSettings).toHaveBeenCalledWith(expect.not.objectContaining({
        openai_api_key: expect.anything(),
      }))
    })
    expect(deleteCredential).not.toHaveBeenCalled()
  })

  it('deletes a cleared Telegram token from the Electron store', async () => {
    const deleteCredential = vi.fn().mockResolvedValue({
      openai: false,
      google: false,
      tavily: false,
      telegramBot: false,
    })
    ;(window as any).electronAPI = {
      isElectron: true,
      applyStoredCredentials: vi.fn().mockResolvedValue({
        openai: false,
        google: false,
        tavily: false,
        telegramBot: true,
      }),
      credentialStatus: vi.fn(),
      setCredential: vi.fn(),
      deleteCredential,
    }

    render(<SettingsModal onClose={() => {}} />)

    fireEvent.click(await screen.findByRole('button', { name: /Connections/i }))
    fireEvent.click(screen.getByRole('button', { name: 'Portal' }))
    fireEvent.click(screen.getByRole('option', { name: 'Telegram' }))
    vi.mocked(updateSettings).mockClear()
    deleteCredential.mockClear()

    fireEvent.click(screen.getByRole('button', { name: 'Remove stored Telegram Bot Token' }))
    fireEvent.click(screen.getByRole('button', { name: /Save/i }))

    await waitFor(() => {
      expect(updateSettings).toHaveBeenCalled()
    })
    expect(updateSettings).not.toHaveBeenCalledWith(expect.objectContaining({
      telegram_bot_token: expect.anything(),
    }))
    expect(deleteCredential).toHaveBeenCalledWith('telegramBot')
  })

  it('saves identity settings with the rest of the runtime settings', async () => {
    render(<SettingsModal onClose={() => {}} />)

    fireEvent.click((await screen.findAllByRole('button', { name: /Identity/i }))[0])
    fireEvent.change(await screen.findByLabelText('Agent name'), { target: { value: 'Hermes' } })
    fireEvent.change(screen.getByLabelText('Your name'), { target: { value: 'Kai' } })
    fireEvent.change(screen.getByLabelText('About you'), {
      target: { value: 'Builder working on Windows automation.' },
    })
    fireEvent.change(screen.getByLabelText('Reply style'), {
      target: { value: 'Use direct, concise replies.' },
    })

    fireEvent.click(screen.getByRole('button', { name: /Save/i }))

    await waitFor(() => {
      expect(updateSettings).toHaveBeenCalledWith(expect.objectContaining({
        identity: {
          agent_name: 'Hermes',
          user_name: 'Kai',
          user_identity: 'Builder working on Windows automation.',
          communication_style: 'Use direct, concise replies.',
        },
      }))
    })
  })

  it('loads and saves custom AGENTS instructions from identity settings', async () => {
    render(<SettingsModal onClose={() => {}} />)

    fireEvent.click((await screen.findAllByRole('button', { name: /Identity/i }))[0])
    expect((await screen.findAllByText(/C:\/Users\/Kai\/.monaw\/workspace\/AGENTS.md/i)).length).toBeGreaterThan(0)
    expect(fetchWorkspaceInstructions).toHaveBeenCalled()

    fireEvent.change(screen.getByLabelText('Custom instructions'), {
      target: { value: '# AGENTS.md\n\n- Prefer focused tests.\n' },
    })
    fireEvent.click(screen.getByRole('button', { name: /Apply custom instructions/i }))

    await waitFor(() => {
      expect(updateWorkspaceInstructions).toHaveBeenCalledWith('# AGENTS.md\n\n- Prefer focused tests.\n')
    })
  })

  it('resets custom AGENTS instructions from identity settings', async () => {
    render(<SettingsModal onClose={() => {}} />)

    fireEvent.click((await screen.findAllByRole('button', { name: /Identity/i }))[0])
    fireEvent.click(await screen.findByRole('button', { name: /Restore default custom instructions/i }))

    await waitFor(() => {
      expect(resetWorkspaceInstructions).toHaveBeenCalled()
    })
    expect(await screen.findByDisplayValue(/Ignore this file if the task is not coding/i)).toBeInTheDocument()
  })

  it('saves the memory switch from the Identity page', async () => {
    render(<SettingsModal onClose={() => {}} />)
    fireEvent.click((await screen.findAllByRole('button', { name: /Identity/i }))[0])

    const memory = await screen.findByRole('checkbox', { name: 'Memory' })
    expect(memory).toBeChecked()
    fireEvent.click(memory)
    fireEvent.click(screen.getByRole('button', { name: /Save/i }))

    await waitFor(() => expect(updateSettings).toHaveBeenCalledWith(expect.objectContaining({
      memory: expect.objectContaining({ enabled: false }),
    })))
  })

  it('has no memory page, request limits, or redundant identity summary', async () => {
    render(<SettingsModal onClose={() => {}} />)
    await screen.findByRole('heading', { name: 'Voice' })
    expect(screen.queryByRole('button', { name: /^Memory$/i })).not.toBeInTheDocument()
    expect(screen.queryByLabelText('Tool iteration limit')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('Turn timeout seconds')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('LLM call timeout seconds')).not.toBeInTheDocument()
    fireEvent.click(screen.getAllByRole('button', { name: /Identity/i })[0])
    expect(screen.queryByText('What the agent will use')).not.toBeInTheDocument()
  })

  it('opens profile help without changing permissions and dismisses it with Escape', async () => {
    vi.mocked(updateSettings).mockClear()
    const onClose = vi.fn()
    render(<SettingsModal onClose={onClose} />)
    fireEvent.click((await screen.findAllByRole('button', { name: /Permissions/i }))[0])

    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument()
    const info = screen.getByRole('button', { name: 'About Full Access' })
    fireEvent.focus(info)
    expect(screen.getByRole('tooltip')).toHaveTextContent('without approval prompts')
    expect(screen.getByRole('button', { name: 'Default' })).toHaveAttribute('aria-pressed', 'true')
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument()
    expect(onClose).not.toHaveBeenCalled()

    fireEvent.click(info)
    expect(screen.getByRole('tooltip')).toBeInTheDocument()
    fireEvent.pointerDown(screen.getByRole('heading', { name: 'Approval profile' }))
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument()
    expect(updateSettings).not.toHaveBeenCalled()
  })

  it('offers exactly four modes without per-action switches', async () => {
    render(<SettingsModal onClose={() => {}} />)
    fireEvent.click((await screen.findAllByRole('button', { name: /Permissions/i }))[0])
    for (const mode of ['Default', 'Full Access', 'Auto Review', 'Custom']) {
      expect(screen.getByRole('button', { name: mode })).toBeInTheDocument()
    }
    expect(screen.queryByRole('switch')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Full Access' })).toHaveAttribute('data-tone', 'danger')
    fireEvent.click(screen.getByRole('button', { name: 'Auto Review' }))
    expect(screen.getByRole('heading', { name: 'Auto Review allows' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /Save/i }))
    await waitFor(() => expect(updateSettings).toHaveBeenCalledWith(expect.objectContaining({
      permissions: expect.objectContaining({ mode: 'auto_review' }),
    })))
  })

  it('restores the saved custom profile after switching through full access', async () => {
    const basePermissions = buildSettings().permissions as any
    const customSettings = buildSettings({
      permissions: {
        ...basePermissions,
        mode: 'custom',
        allow_delete: true,
        allow_screen_fallback: false,
        custom_profile: {
          ...basePermissions.custom_profile,
          allow_delete: true,
          allow_screen_fallback: false,
        },
      },
    })
    vi.mocked(fetchSettings).mockResolvedValue(customSettings as any)
    vi.mocked(updateSettings).mockImplementation(async (payload: any) => ({
      ...customSettings,
      ...payload,
      permissions: payload.permissions ?? customSettings.permissions,
    }) as any)

    render(<SettingsModal onClose={() => {}} />)

    fireEvent.click((await screen.findAllByRole('button', { name: /Permissions/i }))[0])
    fireEvent.click(screen.getByRole('button', { name: 'Full Access' }))
    fireEvent.click(screen.getByRole('button', { name: 'Custom' }))
    fireEvent.click(screen.getByRole('button', { name: /Save/i }))

    await waitFor(() => {
      expect(updateSettings).toHaveBeenCalledWith(expect.objectContaining({
        permissions: expect.objectContaining({
          mode: 'custom',
          allow_delete: true,
          allow_screen_fallback: false,
        }),
      }))
    })
  })

  it('shows Custom rules in a config file instead of setup controls', async () => {
    const customSettings = buildSettings({ permissions_config_path: 'C:/monaw/settings.json' } as any)
    vi.mocked(fetchSettings).mockResolvedValue(customSettings as any)
    render(<SettingsModal onClose={() => {}} />)
    fireEvent.click((await screen.findAllByRole('button', { name: /Permissions/i }))[0])
    fireEvent.click(screen.getByRole('button', { name: 'Custom' }))
    expect(screen.getByRole('heading', { name: 'Custom config' })).toBeInTheDocument()
    expect(screen.getByText('C:/monaw/settings.json')).toBeInTheDocument()
    expect(screen.queryByPlaceholderText('Alias')).not.toBeInTheDocument()
    expect(screen.queryByRole('switch')).not.toBeInTheDocument()
  })

  it('reports a failed save in the footer and keeps the unsaved edit', async () => {
    mocks.updateSettings.mockRejectedValueOnce(new Error('Settings changed in another window.'))
    const onClose = vi.fn()
    render(<SettingsModal onClose={onClose} />)

    fireEvent.click((await screen.findAllByRole('button', { name: /Identity/i }))[0])
    fireEvent.change(await screen.findByLabelText('Your name'), { target: { value: 'Kai' } })
    fireEvent.click(screen.getByRole('button', { name: /Save/i }))

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Settings were not saved. Settings changed in another window.',
    )
    expect(screen.getByLabelText('Your name')).toHaveValue('Kai')
    expect(screen.getByRole('button', { name: /Save changes/i })).toBeEnabled()

    fireEvent.click(screen.getAllByRole('button', { name: 'Cancel' })[0])
    expect(onClose).not.toHaveBeenCalled()
    expect(screen.getByText('Close settings and lose your unsaved changes?')).toBeInTheDocument()
  })

  it('saves sandbox edits made on the Sandbox page', async () => {
    render(<SettingsModal onClose={() => {}} />)

    fireEvent.click((await screen.findAllByRole('button', { name: /Sandbox/i }))[0])
    fireEvent.change(await screen.findByLabelText('Sandbox time limit'), { target: { value: '45' } })
    fireEvent.click(screen.getByRole('button', { name: /Save/i }))

    await waitFor(() => expect(updateSettings).toHaveBeenCalledWith(expect.objectContaining({
      sandbox: expect.objectContaining({ resources: expect.objectContaining({ timeout_seconds: 45 }) }),
    })))
  })
})
