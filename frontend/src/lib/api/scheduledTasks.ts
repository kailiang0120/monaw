import { apiFetch, BASE, JSON_HEADERS, throwApiError } from './client'
import type {
  SchedulePreviewRequest,
  ScheduledTask,
  ScheduledTaskInput,
  ScheduledTaskRun,
  TelegramChatTarget,
} from './types'

export async function fetchScheduledTasks(): Promise<ScheduledTask[]> {
  const res = await apiFetch(`${BASE}/api/scheduled-tasks`)
  if (!res.ok) return throwApiError(res, 'Failed to fetch scheduled tasks')
  return res.json()
}

export async function createScheduledTask(input: ScheduledTaskInput): Promise<ScheduledTask> {
  const res = await apiFetch(`${BASE}/api/scheduled-tasks`, {
    method: 'POST',
    headers: JSON_HEADERS,
    body: JSON.stringify(input),
  })
  if (!res.ok) return throwApiError(res, 'Failed to create scheduled task')
  return res.json()
}

export async function updateScheduledTask(
  id: string,
  patch: Partial<ScheduledTaskInput>,
): Promise<ScheduledTask> {
  const res = await apiFetch(`${BASE}/api/scheduled-tasks/${encodeURIComponent(id)}`, {
    method: 'PATCH',
    headers: JSON_HEADERS,
    body: JSON.stringify(patch),
  })
  if (!res.ok) return throwApiError(res, 'Failed to update scheduled task')
  return res.json()
}

export async function deleteScheduledTask(id: string): Promise<void> {
  const res = await apiFetch(`${BASE}/api/scheduled-tasks/${encodeURIComponent(id)}`, {
    method: 'DELETE',
  })
  if (!res.ok && res.status !== 404) return throwApiError(res, 'Failed to delete scheduled task')
  pendingRuns.delete(id)
}

type RunResult = { runId: number; conversationId: string }
type PendingRun = { key: string; reconcile: boolean; request?: Promise<RunResult> }
// Share an uncertain intention between the sidebar and dialog, across backend
// reconnects. A confirmed response retires it; the next user run gets a new key.
const pendingRuns = new Map<string, PendingRun>()

export function runScheduledTaskNow(id: string): Promise<RunResult> {
  let intention = pendingRuns.get(id)
  if (!intention) {
    intention = { key: crypto.randomUUID(), reconcile: false }
    pendingRuns.set(id, intention)
  }
  if (intention.request) return intention.request
  const current = intention
  current.request = (async () => {
    try {
      if (current.reconcile) {
        const found = await apiFetch(`${BASE}/api/scheduled-tasks/${encodeURIComponent(id)}/operations/${encodeURIComponent(current.key)}`)
        if (found.ok) {
          const result = await found.json() as RunResult
          pendingRuns.delete(id)
          return result
        }
        if (found.status !== 404) return throwApiError(found, 'Could not check the previous run')
      }
      const res = await apiFetch(`${BASE}/api/scheduled-tasks/${encodeURIComponent(id)}/run`, {
        method: 'POST', headers: { 'Idempotency-Key': current.key },
      })
      if (!res.ok) return await throwApiError(res, 'Failed to run scheduled task')
      const result = await res.json() as RunResult
      pendingRuns.delete(id)
      return result
    } catch (error) {
      current.reconcile = true
      throw error
    } finally {
      current.request = undefined
    }
  })()
  return current.request
}

export async function fetchScheduledTaskRuns(id: string, limit = 20): Promise<ScheduledTaskRun[]> {
  const params = new URLSearchParams({ limit: String(limit) })
  const res = await apiFetch(`${BASE}/api/scheduled-tasks/${encodeURIComponent(id)}/runs?${params}`)
  if (!res.ok) return throwApiError(res, 'Failed to fetch scheduled task runs')
  return res.json()
}

export async function previewSchedule(input: SchedulePreviewRequest): Promise<{ next: string[] }> {
  const res = await apiFetch(`${BASE}/api/scheduled-tasks/preview`, {
    method: 'POST',
    headers: JSON_HEADERS,
    body: JSON.stringify(input),
  })
  if (!res.ok) return throwApiError(res, 'Failed to preview schedule')
  return res.json()
}

export async function fetchTelegramChats(): Promise<TelegramChatTarget[]> {
  const res = await apiFetch(`${BASE}/api/scheduled-tasks/telegram-chats`)
  if (!res.ok) return []
  const payload = await res.json()
  if (Array.isArray(payload.chats)) {
    return payload.chats.filter((chat: unknown): chat is TelegramChatTarget => {
      if (!chat || typeof chat !== 'object') return false
      const candidate = chat as Record<string, unknown>
      return typeof candidate.id === 'string' && typeof candidate.label === 'string'
    })
  }
  if (Array.isArray(payload.chatIds)) {
    return payload.chatIds
      .filter((id: unknown): id is string => typeof id === 'string')
      .map((id: string) => ({ id, label: `Telegram - ${id}` }))
  }
  return []
}
