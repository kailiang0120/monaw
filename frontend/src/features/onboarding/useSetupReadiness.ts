import { useEffect, useState } from 'react'

import { fetchBrowserDiagnostics, fetchDiagnosticsSummary } from '../../lib/api/diagnostics'
import { fetchOpenAIAccountStatus, fetchSettings, type OpenAIAccountStatus } from '../../lib/api/settings'
import type { AgentSettings, BrowserUseDiagnostics, DiagnosticsSummary } from '../../lib/api/types'
import { optionalSteps, requiredSteps, type GuideStep } from './readiness'

export interface SetupReadiness {
  loaded: boolean
  required: GuideStep[]
  optional: GuideStep[]
  modelReady: boolean
}

const NOT_LOADED: SetupReadiness = { loaded: false, required: [], optional: [], modelReady: false }

/**
 * Run the checks behind the first-run guide. Each check is fetched only when
 * it matters: the account for the account provider, Chrome for browser control.
 */
export function useSetupReadiness({
  enabled,
  workspaceDir,
  refreshKey,
}: {
  enabled: boolean
  workspaceDir: string
  refreshKey: number
}): SetupReadiness {
  const [settings, setSettings] = useState<AgentSettings | null>(null)
  const [account, setAccount] = useState<OpenAIAccountStatus | null>(null)
  const [summary, setSummary] = useState<DiagnosticsSummary | null>(null)
  const [browser, setBrowser] = useState<BrowserUseDiagnostics | null>(null)

  useEffect(() => {
    if (!enabled) return
    const controller = new AbortController()
    const { signal } = controller
    // A failed check leaves its step "checking"; the guide never blocks the chat.
    const check = async <T>(load: () => Promise<T>, store: (value: T) => void) => {
      try {
        const value = await load()
        if (!signal.aborted) store(value)
      } catch {
        // Unavailable checks are shown as still checking.
      }
    }

    void check(() => fetchSettings(signal), (loaded) => {
      setSettings(loaded)
      if (loaded.llm?.provider === 'codex') void check(() => fetchOpenAIAccountStatus(signal), setAccount)
      if (loaded.tools?.skills?.['browser-use']) void check(() => fetchBrowserDiagnostics(signal), setBrowser)
    })
    void check(() => fetchDiagnosticsSummary(signal), setSummary)

    return () => controller.abort()
  }, [enabled, refreshKey])

  if (!enabled || !settings) return NOT_LOADED
  const required = requiredSteps({ settings, account, workspaceDir })
  return {
    loaded: true,
    required,
    optional: optionalSteps({ settings, summary, browser }),
    modelReady: required.find((step) => step.id === 'model')?.state === 'ready',
  }
}
