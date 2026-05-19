import { BASE, JSON_HEADERS } from './client'
import type {
  AgentSettings,
  AppEntry,
  ControllerPolicy,
  ModelOptions,
  SandboxStatus,
  SettingsUpdatePayload,
  SpeechToTextStatus,
  WorkspaceInstructions,
} from './types'

export async function updateSettings(settings: SettingsUpdatePayload): Promise<AgentSettings> {
  const res = await fetch(`${BASE}/api/settings`, {
    method: 'PUT',
    headers: JSON_HEADERS,
    body: JSON.stringify(settings),
  })
  if (!res.ok) throw new Error('Failed to update settings')
  return res.json()
}

export async function fetchSettings(): Promise<AgentSettings> {
  const res = await fetch(`${BASE}/api/settings`)
  if (!res.ok) throw new Error('Failed to fetch settings')
  return res.json()
}

export async function fetchModelOptions(): Promise<ModelOptions> {
  const res = await fetch(`${BASE}/api/settings/model-options`)
  if (!res.ok) throw new Error('Failed to fetch model options')
  return res.json()
}

export async function fetchWorkspaceInstructions(): Promise<WorkspaceInstructions> {
  const res = await fetch(`${BASE}/api/settings/workspace-instructions`)
  if (!res.ok) throw new Error('Failed to fetch custom instructions')
  return res.json()
}

export async function updateWorkspaceInstructions(content: string): Promise<WorkspaceInstructions> {
  const res = await fetch(`${BASE}/api/settings/workspace-instructions`, {
    method: 'PUT',
    headers: JSON_HEADERS,
    body: JSON.stringify({ content }),
  })
  if (!res.ok) {
    const error = await res.json().catch(() => ({ detail: 'Failed to save custom instructions' }))
    throw new Error(error.detail || 'Failed to save custom instructions')
  }
  return res.json()
}

export async function resetWorkspaceInstructions(): Promise<WorkspaceInstructions> {
  const res = await fetch(`${BASE}/api/settings/workspace-instructions`, { method: 'DELETE' })
  if (!res.ok) {
    const error = await res.json().catch(() => ({ detail: 'Failed to reset custom instructions' }))
    throw new Error(error.detail || 'Failed to reset custom instructions')
  }
  return res.json()
}

export async function fetchSpeechToTextStatus(): Promise<SpeechToTextStatus> {
  const res = await fetch(`${BASE}/api/settings/speech-to-text`)
  if (!res.ok) throw new Error('Failed to fetch speech-to-text status')
  return res.json()
}

export async function downloadSpeechToTextModel(): Promise<SpeechToTextStatus> {
  const res = await fetch(`${BASE}/api/settings/speech-to-text/download`, { method: 'POST' })
  if (!res.ok) {
    const error = await res.json().catch(() => ({ detail: 'Failed to download speech-to-text model' }))
    throw new Error(error.detail || 'Failed to download speech-to-text model')
  }
  return res.json()
}

export async function offloadSpeechToTextModel(): Promise<SpeechToTextStatus> {
  const res = await fetch(`${BASE}/api/settings/speech-to-text/offload`, { method: 'POST' })
  if (!res.ok) {
    const error = await res.json().catch(() => ({ detail: 'Failed to offload speech-to-text model' }))
    throw new Error(error.detail || 'Failed to offload speech-to-text model')
  }
  return res.json()
}

export async function deleteSpeechToTextModel(): Promise<SpeechToTextStatus> {
  const res = await fetch(`${BASE}/api/settings/speech-to-text/model`, { method: 'DELETE' })
  if (!res.ok) {
    const error = await res.json().catch(() => ({ detail: 'Failed to delete speech-to-text model' }))
    throw new Error(error.detail || 'Failed to delete speech-to-text model')
  }
  return res.json()
}

export async function fetchSandboxStatus(): Promise<SandboxStatus> {
  const res = await fetch(`${BASE}/api/sandbox/status`)
  if (!res.ok) throw new Error('Failed to fetch sandbox status')
  return res.json()
}

export async function fetchControllerPolicy(): Promise<ControllerPolicy> {
  const res = await fetch(`${BASE}/api/settings/controller-policy`)
  if (!res.ok) throw new Error('Failed to fetch controller policy')
  return res.json()
}

export async function updateControllerPolicy(body: Partial<ControllerPolicy>): Promise<void> {
  await fetch(`${BASE}/api/settings/controller-policy`, {
    method: 'PUT',
    headers: JSON_HEADERS,
    body: JSON.stringify(body),
  })
}

export async function fetchAllowlistedApps(): Promise<AppEntry[]> {
  const res = await fetch(`${BASE}/api/settings/allowlisted-apps`)
  if (!res.ok) throw new Error('Failed to fetch allowlisted apps')
  return res.json()
}

export async function addAllowlistedApp(entry: {
  alias: string
  display_name: string
  exe_paths: string[]
}): Promise<AppEntry> {
  const res = await fetch(`${BASE}/api/settings/allowlisted-apps`, {
    method: 'POST',
    headers: JSON_HEADERS,
    body: JSON.stringify(entry),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: 'Failed' }))
    throw new Error(err.detail || 'Failed to add app')
  }
  return res.json()
}

export async function removeAllowlistedApp(alias: string): Promise<void> {
  await fetch(`${BASE}/api/settings/allowlisted-apps/${alias}`, { method: 'DELETE' })
}
