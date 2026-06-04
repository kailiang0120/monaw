import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  fetchModelOptions,
  fetchSettings,
  fetchWorkspaceInstructions,
  resetWorkspaceInstructions,
  updateSettings,
  updateWorkspaceInstructions,
} from '../lib/api/settings'
import {
  fetchMemoryCandidates,
  fetchMemoryCheckpoints,
  fetchMemoryEpisodes,
  fetchMemories,
  fetchMemoryFile,
  fetchMemoryProfile,
  fetchMemoryStats,
} from '../lib/api/memories'
import { SettingsModal } from '../features/settings/SettingsModal'

const mocks = vi.hoisted(() => ({
  fetchSettings: vi.fn(),
  fetchModelOptions: vi.fn(),
  fetchSandboxStatus: vi.fn(),
  fetchSpeechToTextStatus: vi.fn(),
  fetchWorkspaceInstructions: vi.fn(),
  downloadSpeechToTextModel: vi.fn(),
  offloadSpeechToTextModel: vi.fn(),
  deleteSpeechToTextModel: vi.fn(),
  updateWorkspaceInstructions: vi.fn(),
  resetWorkspaceInstructions: vi.fn(),
  updateSettings: vi.fn(),
  fetchMemories: vi.fn(),
  fetchMemoryFile: vi.fn(),
  fetchMemoryStats: vi.fn(),
  fetchMemoryProfile: vi.fn(),
  fetchMemoryCandidates: vi.fn(),
  fetchMemoryEpisodes: vi.fn(),
  fetchMemoryCheckpoints: vi.fn(),
  searchMemories: vi.fn(),
  createMemory: vi.fn(),
  updateMemory: vi.fn(),
  deleteMemory: vi.fn(),
  updateMemoryCandidate: vi.fn(),
}))

vi.mock('../lib/api/diagnostics', () => ({
  fetchBrowserUseDiagnostics: vi.fn().mockResolvedValue({
    available: true,
    skill_enabled: true,
    skill_available: true,
    skill_unavailable_reason: '',
    session_active: false,
    preferred_mode: 'auto',
    current_mode: '',
    current_system_connection: '',
    fallback_enabled: true,
    last_error: '',
    system_connection_strategy: 'auto',
    system_cdp_url: 'http://127.0.0.1:9222',
    managed_profile_dir: 'C:/runtime/browser/managed-profile',
    downloads_dir: 'C:/runtime/browser/downloads',
    screenshots_dir: 'C:/runtime/browser/screenshots',
    traces_dir: 'C:/runtime/browser/traces',
    system_profile_directory: '',
    chrome_executable: 'C:/Program Files/Google/Chrome/Application/chrome.exe',
    available_system_profiles: [],
    current_page: null,
    tab_count: 0,
  }),
  fetchMCPDiagnostics: vi.fn().mockResolvedValue([]),
  resetBrowserUseSession: vi.fn(),
  reconnectMCPServer: vi.fn(),
}))

vi.mock('../lib/api/settings', () => ({
  fetchSettings: mocks.fetchSettings,
  fetchModelOptions: mocks.fetchModelOptions,
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

vi.mock('../lib/api/memories', () => ({
  fetchMemories: mocks.fetchMemories,
  fetchMemoryFile: mocks.fetchMemoryFile,
  fetchMemoryStats: mocks.fetchMemoryStats,
  fetchMemoryProfile: mocks.fetchMemoryProfile,
  fetchMemoryCandidates: mocks.fetchMemoryCandidates,
  fetchMemoryEpisodes: mocks.fetchMemoryEpisodes,
  fetchMemoryCheckpoints: mocks.fetchMemoryCheckpoints,
  searchMemories: mocks.searchMemories,
  createMemory: mocks.createMemory,
  updateMemory: mocks.updateMemory,
  deleteMemory: mocks.deleteMemory,
  saveMemoryFile: vi.fn(),
  updateSection: vi.fn(),
  updateMemoryCandidate: mocks.updateMemoryCandidate,
}))

function buildMemoryStats(overrides: Record<string, unknown> = {}) {
  return {
    total: 0,
    active: 0,
    archived: 0,
    new: 0,
    reviewed: 0,
    fact: 0,
    reflection: 0,
    candidates: 0,
    unresolved_candidates: 0,
    short_term: 0,
    personalities: 0,
    curated_sessions: 0,
    audit_events: 0,
    archived_messages: 0,
    memory_root: 'C:/runtime/memory',
    ...overrides,
  }
}

function buildMemoryRecord(overrides: Record<string, unknown> = {}) {
  return {
    id: 'memory-1',
    content: 'Memory one',
    category: 'fact',
    status: 'active',
    review_state: 'new',
    confidence: 0.9,
    importance: 5,
    kind: 'fact',
    source: 'memory_curator',
    source_conversation_id: 'conv-1',
    source_message_id: null,
    created_at: '2026-05-08T00:00:00Z',
    updated_at: '2026-05-08T00:00:00Z',
    last_used_at: '',
    use_count: 0,
    ...overrides,
  }
}

function buildSettings(overrides: Record<string, unknown> = {}) {
  return {
    llm: {
      provider: 'openai',
      model_name: 'gpt-5.4',
      reasoning_effort: 'medium',
      vision_fallback_enabled: true,
      vision_fallback_model: 'gemini-3.1-flash-lite-preview',
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
      mode: 'auto',
      enable_system_fallback: true,
      headless: false,
      keep_alive: true,
      system_connection_strategy: 'auto',
      system_cdp_url: 'http://127.0.0.1:9222',
      managed_profile_dir: 'C:/runtime/browser/managed-profile',
      downloads_dir: 'C:/runtime/browser/downloads',
      screenshots_dir: 'C:/runtime/browser/screenshots',
      traces_dir: 'C:/runtime/browser/traces',
      system_profile_directory: '',
      allowed_domains: [],
    },
    memory: {
      enabled: true,
      auto_learn: true,
      curate_on_session_close: true,
      write_policy: 'auto_with_review',
      retrieval_limit: 6,
      max_injected_chars: 2500,
      min_confidence: 0.75,
      min_relevance_score: 0.15,
      maintenance_cooldown_hours: 24,
    },
    tools: {
      skills: {
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
    available_skills: [
      {
        slug: 'browser_use',
        name: 'browser-use',
        description: 'Browser automation',
        version: '1.0.0',
        enabled_by_default: true,
        enabled: true,
        available: true,
        always: false,
        unavailable_reason: '',
        tier: 'recommended',
        recommended: true,
      },
      {
        slug: 'core',
        name: 'core',
        description: 'Core tools',
        version: '1.0.0',
        enabled_by_default: true,
        enabled: true,
        available: true,
        always: false,
        unavailable_reason: '',
        tier: 'recommended',
        recommended: true,
      },
      {
        slug: 'exec',
        name: 'exec',
        description: 'Exec tools',
        version: '1.0.0',
        enabled_by_default: true,
        enabled: true,
        available: true,
        always: false,
        unavailable_reason: '',
        tier: 'recommended',
        recommended: true,
      },
      {
        slug: 'computer_use',
        name: 'computer-use',
        description: 'Computer use',
        version: '1.0.0',
        enabled_by_default: true,
        enabled: true,
        available: true,
        always: false,
        unavailable_reason: '',
        tier: 'recommended',
        recommended: true,
      },
      {
        slug: 'filesystem',
        name: 'filesystem',
        description: 'Filesystem tools',
        version: '1.0.0',
        enabled_by_default: true,
        enabled: true,
        available: true,
        always: false,
        unavailable_reason: '',
        tier: 'recommended',
        recommended: true,
      },
      {
        slug: 'memory',
        name: 'memory',
        description: 'Long-term memory',
        version: '1.0.0',
        enabled_by_default: true,
        enabled: true,
        available: true,
        always: false,
        unavailable_reason: '',
        tier: 'recommended',
        recommended: true,
      },
      {
        slug: 'mcp_bridge',
        name: 'mcp-bridge',
        description: 'MCP bridge',
        version: '1.0.0',
        enabled_by_default: false,
        enabled: false,
        available: true,
        always: false,
        unavailable_reason: '',
        tier: 'optional',
        recommended: false,
      },
    ],
    api_keys: {
      has_openai_key: true,
      has_deepseek_key: true,
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
    vi.mocked(fetchSettings).mockResolvedValue(buildSettings() as any)
    vi.mocked(fetchModelOptions).mockResolvedValue({
      providers: [
        { id: 'openai', label: 'OpenAI', models: ['gpt-5.5', 'gpt-5.4', 'gpt-5.4-mini', 'gpt-5.4-nano'] },
        { id: 'deepseek', label: 'DeepSeek', models: ['deepseek-v4-flash', 'deepseek-v4-pro'] },
        {
          id: 'gemini',
          label: 'Google',
          models: ['gemini-3.1-pro-preview', 'gemini-3.1-flash-lite', 'gemini-3.1-flash-lite-preview', 'gemini-3-flash-preview'],
        },
      ],
      vision_fallback_models: ['gemini-3.1-flash-lite-preview', 'gemini-3.1-pro-preview'],
    } as any)
    mocks.fetchSandboxStatus.mockResolvedValue({
      enabled: true,
      mode: 'auto',
      default_profile: 'standard',
      default_network: 'deny',
      default_write_strategy: 'copy_out',
      require_strong_for_untrusted: true,
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
    vi.mocked(fetchMemories).mockResolvedValue([])
    vi.mocked(fetchMemoryFile).mockResolvedValue(null)
    vi.mocked(fetchMemoryStats).mockResolvedValue(buildMemoryStats() as any)
    vi.mocked(fetchMemoryProfile).mockResolvedValue([])
    vi.mocked(fetchMemoryCandidates).mockResolvedValue([])
    vi.mocked(fetchMemoryEpisodes).mockResolvedValue([])
    vi.mocked(fetchMemoryCheckpoints).mockResolvedValue([])
    mocks.searchMemories.mockResolvedValue([])
    mocks.createMemory.mockResolvedValue(buildMemoryRecord())
    mocks.updateMemory.mockResolvedValue(buildMemoryRecord())
    mocks.deleteMemory.mockResolvedValue(undefined)
    mocks.updateMemoryCandidate.mockResolvedValue({
      id: 'candidate-1',
      category: 'preference',
      content: 'Candidate memory',
      confidence: 0.9,
      importance: 5,
      kind: 'fact',
      status: 'approved',
      reason: 'turn_feature_extract',
      source_conversation_id: 'conv-1',
      source_message_id: null,
      created_at: '2026-05-08T00:00:00Z',
      updated_at: '2026-05-08T00:00:00Z',
    })
    delete (window as any).electronAPI
  })

  afterEach(() => {
    cleanup()
  })

  it('shows Google chat models separately from vision fallback models', async () => {
    render(<SettingsModal onClose={() => {}} />)

    expect(await screen.findByRole('heading', { name: 'Model' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Provider' }))
    fireEvent.click(screen.getByRole('button', { name: 'Google' }))
    expect(screen.getByText('gemini-3.1-pro-preview')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Reasoning effort' }))
    expect(screen.getByRole('button', { name: 'High' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Medium' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Minimal' })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'High' }))
    const modelButtons = screen.getAllByRole('button', { name: 'Model' })
    fireEvent.click(modelButtons[modelButtons.length - 1])
    expect(screen.getByRole('button', { name: 'gemini-3-flash-preview' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'gemini-3-flash-preview' }))
    fireEvent.click(screen.getByRole('button', { name: 'Reasoning effort' }))
    expect(screen.getByRole('button', { name: 'Minimal' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Medium' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /Connections/i }))
    expect(screen.getByText(/Google key saved: yes/i)).toBeInTheDocument()
  })

  it('saves Google as a primary Gemini chat provider', async () => {
    render(<SettingsModal onClose={() => {}} />)

    await screen.findByRole('heading', { name: 'Model' })
    fireEvent.click(screen.getByRole('button', { name: 'Provider' }))
    fireEvent.click(screen.getByRole('button', { name: 'Google' }))
    const modelButtons = screen.getAllByRole('button', { name: 'Model' })
    fireEvent.click(modelButtons[modelButtons.length - 1])
    fireEvent.click(screen.getByRole('button', { name: 'gemini-3-flash-preview' }))
    fireEvent.click(screen.getByRole('button', { name: /Save/i }))

    await waitFor(() => {
      expect(updateSettings).toHaveBeenCalledWith(expect.objectContaining({
        llm: expect.objectContaining({
          provider: 'gemini',
          model_name: 'gemini-3-flash-preview',
          reasoning_effort: 'medium',
        }),
      }))
    })
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

  it('uses Electron-stored keys for backend sync and visible key status', async () => {
    vi.mocked(fetchSettings).mockResolvedValue(buildSettings({
      api_keys: {
        has_openai_key: false,
        has_deepseek_key: false,
        has_google_key: false,
        has_tavily_key: false,
        has_telegram_bot_token: false,
      },
    }) as any)
    ;(window as any).electronAPI = {
      isElectron: true,
      storeGet: vi.fn((key: string) => Promise.resolve({
        openai_api_key: 'stored-openai',
        deepseek_api_key: 'stored-deepseek',
        google_api_key: 'stored-google',
        tavily_api_key: 'stored-tavily',
        telegram_bot_token: 'stored-telegram',
        telegram_allowed_user_ids: '123456789',
      }[key] ?? '')),
      storeSet: vi.fn(),
      storeDelete: vi.fn(),
    }

    render(<SettingsModal onClose={() => {}} />)

    fireEvent.click(await screen.findByRole('button', { name: /Connections/i }))
    expect(await screen.findByText(/OpenAI key saved: yes/i)).toBeInTheDocument()
    expect(screen.getAllByText(/Google key saved: yes/i).length).toBeGreaterThan(0)
    fireEvent.click(screen.getByRole('button', { name: 'Portal' }))
    fireEvent.click(screen.getByRole('button', { name: 'Telegram' }))
    expect(screen.getByText(/Telegram token saved: yes/i)).toBeInTheDocument()
    await waitFor(() => {
      expect(updateSettings).toHaveBeenCalledWith({
        openai_api_key: 'stored-openai',
        deepseek_api_key: 'stored-deepseek',
        google_api_key: 'stored-google',
        tavily_api_key: 'stored-tavily',
        telegram_bot_token: 'stored-telegram',
        telegram_allowed_user_ids: '123456789',
      })
    })
  })

  it('saves Telegram allowlist fields from the Telegram connection portal', async () => {
    const storeSet = vi.fn()
    ;(window as any).electronAPI = {
      isElectron: true,
      storeGet: vi.fn(() => Promise.resolve('')),
      storeSet,
      storeDelete: vi.fn(),
    }

    render(<SettingsModal onClose={() => {}} />)

    fireEvent.click(await screen.findByRole('button', { name: /Connections/i }))
    fireEvent.click(screen.getByRole('button', { name: 'Portal' }))
    fireEvent.click(screen.getByRole('button', { name: 'Telegram' }))
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
    expect(storeSet).toHaveBeenCalledWith('telegram_allowed_user_ids', '123456789')
    expect(storeSet).toHaveBeenCalledWith('telegram_allowed_chat_ids', '-1001234567890')
  })

  it('does not delete saved Electron keys when secret fields were not edited', async () => {
    vi.mocked(fetchSettings).mockResolvedValue(buildSettings({
      api_keys: {
        has_openai_key: true,
        has_deepseek_key: true,
        has_google_key: true,
        has_tavily_key: true,
        has_telegram_bot_token: true,
      },
    }) as any)
    const storeDelete = vi.fn()
    ;(window as any).electronAPI = {
      isElectron: true,
      storeGet: vi.fn(() => Promise.resolve('')),
      storeSet: vi.fn(),
      storeDelete,
    }

    render(<SettingsModal onClose={() => {}} />)

    fireEvent.click((await screen.findAllByRole('button', { name: /Identity/i }))[0])
    fireEvent.change(await screen.findByLabelText('Call me'), { target: { value: 'Kai' } })
    fireEvent.click(screen.getByRole('button', { name: /Save/i }))

    await waitFor(() => {
      expect(updateSettings).toHaveBeenCalledWith(expect.not.objectContaining({
        openai_api_key: expect.anything(),
      }))
    })
    expect(storeDelete).not.toHaveBeenCalled()
  })

  it('deletes a cleared Telegram token from the Electron store', async () => {
    const storeDelete = vi.fn()
    ;(window as any).electronAPI = {
      isElectron: true,
      storeGet: vi.fn((key: string) => Promise.resolve({
        telegram_bot_token: 'stored-telegram',
      }[key] ?? '')),
      storeSet: vi.fn(),
      storeDelete,
    }

    render(<SettingsModal onClose={() => {}} />)

    fireEvent.click(await screen.findByRole('button', { name: /Connections/i }))
    fireEvent.click(screen.getByRole('button', { name: 'Portal' }))
    fireEvent.click(screen.getByRole('button', { name: 'Telegram' }))
    await waitFor(() => {
      expect(updateSettings).toHaveBeenCalledWith(expect.objectContaining({
        telegram_bot_token: 'stored-telegram',
      }))
    })

    vi.mocked(updateSettings).mockClear()
    storeDelete.mockClear()

    fireEvent.change(screen.getByPlaceholderText('Telegram Bot Token'), {
      target: { value: '' },
    })
    fireEvent.click(screen.getByRole('button', { name: /Save/i }))

    await waitFor(() => {
      expect(updateSettings).toHaveBeenCalledWith(expect.objectContaining({
        telegram_bot_token: '',
      }))
    })
    expect(storeDelete).toHaveBeenCalledWith('telegram_bot_token')
  })

  it('saves identity settings with the rest of the runtime settings', async () => {
    render(<SettingsModal onClose={() => {}} />)

    fireEvent.click((await screen.findAllByRole('button', { name: /Identity/i }))[0])
    fireEvent.change(await screen.findByLabelText('Agent nickname'), { target: { value: 'Hermes' } })
    fireEvent.change(screen.getByLabelText('Call me'), { target: { value: 'Kai' } })
    fireEvent.change(screen.getByLabelText('User identity'), {
      target: { value: 'Builder working on Windows automation.' },
    })
    fireEvent.change(screen.getByLabelText('Communication style'), {
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

  it('refreshes memory stats and records from the memory settings panel', async () => {
    vi.mocked(fetchMemories)
      .mockResolvedValueOnce([buildMemoryRecord({ id: 'memory-1', content: 'Memory one' }) as any])
      .mockResolvedValueOnce([buildMemoryRecord({ id: 'memory-2', content: 'Memory two' }) as any])
    vi.mocked(fetchMemoryStats)
      .mockResolvedValueOnce(buildMemoryStats({
        active: 1,
        new: 1,
        short_term: 1,
        personalities: 2,
        curated_sessions: 1,
        audit_events: 3,
      }) as any)
      .mockResolvedValueOnce(buildMemoryStats({
        active: 2,
        new: 2,
        short_term: 2,
        personalities: 2,
        curated_sessions: 2,
        audit_events: 4,
      }) as any)

    render(<SettingsModal onClose={() => {}} />)

    fireEvent.click((await screen.findAllByRole('button', { name: /Memory/i }))[0])
    expect((await screen.findAllByText('Memory one')).length).toBeGreaterThan(0)
    expect(screen.getByText('Candidates')).toBeInTheDocument()
    expect(screen.getByText('Episodes')).toBeInTheDocument()
    expect(screen.getByText('Context')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /Refresh/i }))

    expect((await screen.findAllByText('Memory two')).length).toBeGreaterThan(0)
    expect(fetchMemories).toHaveBeenCalledTimes(2)
    expect(fetchMemoryStats).toHaveBeenCalledTimes(2)
  })

  it('shows a memory skeleton while the memory panel is loading', async () => {
    let resolveMemories: ((value: any) => void) | null = null
    vi.mocked(fetchMemories).mockReturnValue(
      new Promise((resolve) => {
        resolveMemories = resolve
      }) as any,
    )
    vi.mocked(fetchMemoryStats).mockResolvedValue(buildMemoryStats() as any)
    vi.mocked(fetchMemoryFile).mockResolvedValue(null as any)

    render(<SettingsModal onClose={() => {}} />)

    fireEvent.click((await screen.findAllByRole('button', { name: /Memory/i }))[0])
    expect(await screen.findByTestId('memory-loading-skeleton')).toBeInTheDocument()

    resolveMemories?.([buildMemoryRecord({ id: 'memory-loaded', content: 'Loaded memory' })])

    expect((await screen.findAllByText('Loaded memory')).length).toBeGreaterThan(0)
  })

  it('does not submit manual memories shorter than the backend quality gate', async () => {
    render(<SettingsModal onClose={() => {}} />)

    fireEvent.click((await screen.findAllByRole('button', { name: /Memory/i }))[0])
    fireEvent.change(await screen.findByPlaceholderText('Add a memory…'), {
      target: { value: 'Hey' },
    })

    expect(screen.getByText(/Use at least 8 characters/i)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Add memory' })).toBeDisabled()
    expect(mocks.createMemory).not.toHaveBeenCalled()
  })

  it('saves runtime iteration limits from model settings', async () => {
    render(<SettingsModal onClose={() => {}} />)

    fireEvent.change(await screen.findByLabelText('Tool iteration limit'), {
      target: { value: '180' },
    })
    fireEvent.click(screen.getByRole('button', { name: /Save/i }))

    await waitFor(() => {
      expect(updateSettings).toHaveBeenCalledWith(expect.objectContaining({
        llm: expect.objectContaining({
          max_iterations_per_turn: 180,
          max_turn_seconds: 1800,
          max_llm_call_seconds: 300,
        }),
      }))
    })
  })

  it('saves vision fallback settings from model settings', async () => {
    render(<SettingsModal onClose={() => {}} />)

    fireEvent.click(await screen.findByLabelText('Vision fallback'))
    fireEvent.click(screen.getByRole('button', { name: /Save/i }))

    await waitFor(() => {
      expect(updateSettings).toHaveBeenCalledWith(expect.objectContaining({
        llm: expect.objectContaining({
          vision_fallback_enabled: false,
          vision_fallback_model: 'gemini-3.1-flash-lite-preview',
        }),
      }))
    })
  })

  it('switches preset permissions to custom when a preset toggle changes', async () => {
    const fullAccessSettings = buildSettings({
      permissions: {
        ...(buildSettings().permissions as any),
        mode: 'full_access',
        confirmations: {
          mutate: false,
          delete: true,
          launch_app: false,
          click: false,
          type: false,
        },
        dangerous_actions_require_confirm: false,
        allow_screen_fallback: true,
      },
    })
    vi.mocked(fetchSettings).mockResolvedValue(fullAccessSettings as any)
    vi.mocked(updateSettings).mockImplementation(async (payload: any) => ({
      ...fullAccessSettings,
      ...payload,
      permissions: payload.permissions ?? fullAccessSettings.permissions,
    }) as any)

    render(<SettingsModal onClose={() => {}} />)

    fireEvent.click((await screen.findAllByRole('button', { name: /Permissions/i }))[0])
    fireEvent.click(await screen.findByLabelText('Allow screen fallback'))
    fireEvent.click(screen.getByRole('button', { name: /Save/i }))

    await waitFor(() => {
      expect(updateSettings).toHaveBeenCalledWith(expect.objectContaining({
        permissions: expect.objectContaining({
          mode: 'custom',
          allow_screen_fallback: false,
        }),
      }))
    })
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

  it('keeps app override alias input focused while typing', async () => {
    vi.mocked(fetchSettings).mockResolvedValue(buildSettings({
      permissions: {
        ...(buildSettings().permissions as any),
        app_rules: [
          {
            alias: '',
            display_name: '',
            exe_paths: [],
            launch_allowed: true,
            uia_allowed: true,
            screen_fallback_allowed: false,
            require_confirmation: false,
            enabled: true,
          },
        ],
      },
    }) as any)

    render(<SettingsModal onClose={() => {}} />)

    fireEvent.click((await screen.findAllByRole('button', { name: /Permissions/i }))[0])
    const aliasInput = await screen.findByPlaceholderText('Alias')
    aliasInput.focus()
    fireEvent.change(aliasInput, { target: { value: 't' } })
    fireEvent.change(aliasInput, { target: { value: 'te' } })
    fireEvent.change(aliasInput, { target: { value: 'tes' } })
    fireEvent.change(aliasInput, { target: { value: 'test' } })

    expect(screen.getByPlaceholderText('Alias')).toHaveValue('test')
    expect(document.activeElement).toBe(screen.getByPlaceholderText('Alias'))
  })
})
