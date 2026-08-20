import { apiFetch, BASE, JSON_HEADERS, throwApiError } from './client'
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
  const res = await apiFetch(`${BASE}/api/settings`, {
    method: 'PUT',
    headers: JSON_HEADERS,
    body: JSON.stringify(settings),
  })
  if (!res.ok) await throwApiError(res, 'Failed to update settings')
  return res.json()
}

export async function fetchSettings(signal?: AbortSignal): Promise<AgentSettings> {
  const res = await apiFetch(`${BASE}/api/settings`, { signal })
  if (!res.ok) await throwApiError(res, 'Failed to fetch settings')
  return res.json()
}

export async function fetchModelOptions(signal?: AbortSignal): Promise<ModelOptions> {
  const res = await apiFetch(`${BASE}/api/settings/model-options`, { signal })
  if (!res.ok) await throwApiError(res, 'Failed to fetch model options')
  return res.json()
}

export async function fetchWorkspaceInstructions(signal?: AbortSignal): Promise<WorkspaceInstructions> {
  const res = await apiFetch(`${BASE}/api/settings/workspace-instructions`, { signal })
  if (!res.ok) await throwApiError(res, 'Failed to fetch custom instructions')
  return res.json()
}

export async function updateWorkspaceInstructions(content: string): Promise<WorkspaceInstructions> {
  const res = await apiFetch(`${BASE}/api/settings/workspace-instructions`, {
    method: 'PUT',
    headers: JSON_HEADERS,
    body: JSON.stringify({ content }),
  })
  if (!res.ok) await throwApiError(res, 'Failed to save custom instructions')
  return res.json()
}

export async function resetWorkspaceInstructions(): Promise<WorkspaceInstructions> {
  const res = await apiFetch(`${BASE}/api/settings/workspace-instructions`, { method: 'DELETE' })
  if (!res.ok) await throwApiError(res, 'Failed to reset custom instructions')
  return res.json()
}

export async function fetchSpeechToTextStatus(signal?: AbortSignal): Promise<SpeechToTextStatus> {
  const res = await apiFetch(`${BASE}/api/settings/speech-to-text`, { signal })
  if (!res.ok) await throwApiError(res, 'Failed to fetch speech-to-text status')
  return res.json()
}

export async function downloadSpeechToTextModel(): Promise<SpeechToTextStatus> {
  const res = await apiFetch(`${BASE}/api/settings/speech-to-text/download`, { method: 'POST' })
  if (!res.ok) await throwApiError(res, 'Failed to download speech-to-text model')
  return res.json()
}

export async function offloadSpeechToTextModel(): Promise<SpeechToTextStatus> {
  const res = await apiFetch(`${BASE}/api/settings/speech-to-text/offload`, { method: 'POST' })
  if (!res.ok) await throwApiError(res, 'Failed to offload speech-to-text model')
  return res.json()
}

export async function deleteSpeechToTextModel(): Promise<SpeechToTextStatus> {
  const res = await apiFetch(`${BASE}/api/settings/speech-to-text/model`, { method: 'DELETE' })
  if (!res.ok) await throwApiError(res, 'Failed to delete speech-to-text model')
  return res.json()
}

export async function fetchSandboxStatus(signal?: AbortSignal): Promise<SandboxStatus> {
  const res = await apiFetch(`${BASE}/api/sandbox/status`, { signal })
  if (!res.ok) await throwApiError(res, 'Failed to fetch sandbox status')
  return res.json()
}

export async function resolveSandboxDockerImage(image: string): Promise<{
  image: string
  detail: string
  sandbox: SandboxStatus
}> {
  const res = await apiFetch(`${BASE}/api/sandbox/docker/resolve-image`, {
    method: 'POST',
    headers: JSON_HEADERS,
    body: JSON.stringify({ image }),
  })
  if (!res.ok) await throwApiError(res, 'Failed to resolve Docker image')
  return res.json()
}

export async function fetchControllerPolicy(): Promise<ControllerPolicy> {
  const res = await apiFetch(`${BASE}/api/settings/controller-policy`)
  if (!res.ok) await throwApiError(res, 'Failed to fetch controller policy')
  return res.json()
}

export async function updateControllerPolicy(body: Partial<ControllerPolicy>): Promise<void> {
  const res = await apiFetch(`${BASE}/api/settings/controller-policy`, {
    method: 'PUT',
    headers: JSON_HEADERS,
    body: JSON.stringify(body),
  })
  if (!res.ok) await throwApiError(res, 'Failed to update controller policy')
}

export async function fetchAllowlistedApps(): Promise<AppEntry[]> {
  const res = await apiFetch(`${BASE}/api/settings/allowlisted-apps`)
  if (!res.ok) await throwApiError(res, 'Failed to fetch allowlisted apps')
  return res.json()
}

export async function addAllowlistedApp(entry: {
  alias: string
  display_name: string
  exe_paths: string[]
}): Promise<AppEntry> {
  const res = await apiFetch(`${BASE}/api/settings/allowlisted-apps`, {
    method: 'POST',
    headers: JSON_HEADERS,
    body: JSON.stringify(entry),
  })
  if (!res.ok) await throwApiError(res, 'Failed to add app')
  return res.json()
}

export async function removeAllowlistedApp(alias: string): Promise<void> {
  const res = await apiFetch(`${BASE}/api/settings/allowlisted-apps/${alias}`, { method: 'DELETE' })
  if (!res.ok) await throwApiError(res, 'Failed to remove app')
}
