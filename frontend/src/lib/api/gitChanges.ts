import { apiFetch, BASE, JSON_HEADERS, decodeApiError } from './client'

export interface GitChange {
  path: string
  action: string
  added: number | null
  removed: number | null
  undo: string
  diff?: string
  truncated?: boolean
}
export interface GitChanges {
  files: GitChange[]
  unavailable?: string
  excluded_count: number
}
async function request<T>(runId: string, suffix: string, init?: RequestInit): Promise<T> {
  const response = await apiFetch(`${BASE}/api/runs/${encodeURIComponent(runId)}/changes${suffix}`, init)
  if (!response.ok) throw await decodeApiError(response, 'Unable to load workspace changes')
  return response.json() as Promise<T>
}
export function fetchGitChanges(runId: string, conversationId: string, path?: string, signal?: AbortSignal) {
  const params = new URLSearchParams({ conversation_id: conversationId })
  if (path) params.set('path', path)
  return request<GitChanges>(runId, `?${params}`, { signal })
}
export function previewGitUndo(runId: string, conversationId: string, path: string) {
  return request<{ confirmation_id: string; file: GitChange }>(runId, '/undo-preview', {
    method: 'POST', headers: JSON_HEADERS, body: JSON.stringify({ conversation_id: conversationId, path }),
  })
}
export function applyGitUndo(runId: string, conversationId: string, path: string, confirmationId: string) {
  return request(runId, '/undo', {
    method: 'POST', headers: JSON_HEADERS,
    body: JSON.stringify({ conversation_id: conversationId, path, confirmation_id: confirmationId }),
  })
}
