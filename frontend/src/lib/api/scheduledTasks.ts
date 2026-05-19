import { BASE, JSON_HEADERS } from './client'
import type {
  SchedulePreviewRequest,
  ScheduledTask,
  ScheduledTaskInput,
  ScheduledTaskRun,
  TelegramChatTarget,
} from './types'

export async function fetchScheduledTasks(): Promise<ScheduledTask[]> {
  const res = await fetch(`${BASE}/api/scheduled-tasks`)
  if (!res.ok) throw new Error('Failed to fetch scheduled tasks')
  return res.json()
}

export async function createScheduledTask(input: ScheduledTaskInput): Promise<ScheduledTask> {
  const res = await fetch(`${BASE}/api/scheduled-tasks`, {
    method: 'POST',
    headers: JSON_HEADERS,
    body: JSON.stringify(input),
  })
  if (!res.ok) throw new Error(await errorText(res, 'Failed to create scheduled task'))
  return res.json()
}

export async function updateScheduledTask(
  id: string,
  patch: Partial<ScheduledTaskInput>,
): Promise<ScheduledTask> {
  const res = await fetch(`${BASE}/api/scheduled-tasks/${encodeURIComponent(id)}`, {
    method: 'PATCH',
    headers: JSON_HEADERS,
    body: JSON.stringify(patch),
  })
  if (!res.ok) throw new Error(await errorText(res, 'Failed to update scheduled task'))
  return res.json()
}

export async function deleteScheduledTask(id: string): Promise<void> {
  const res = await fetch(`${BASE}/api/scheduled-tasks/${encodeURIComponent(id)}`, {
    method: 'DELETE',
  })
  if (!res.ok) throw new Error('Failed to delete scheduled task')
}

export async function runScheduledTaskNow(id: string): Promise<{ runId: number; conversationId: string }> {
  const res = await fetch(`${BASE}/api/scheduled-tasks/${encodeURIComponent(id)}/run`, {
    method: 'POST',
  })
  if (!res.ok) throw new Error(await errorText(res, 'Failed to run scheduled task'))
  return res.json()
}

export async function fetchScheduledTaskRuns(id: string, limit = 20): Promise<ScheduledTaskRun[]> {
  const params = new URLSearchParams({ limit: String(limit) })
  const res = await fetch(`${BASE}/api/scheduled-tasks/${encodeURIComponent(id)}/runs?${params}`)
  if (!res.ok) throw new Error('Failed to fetch scheduled task runs')
  return res.json()
}

export async function previewSchedule(input: SchedulePreviewRequest): Promise<{ next: string[] }> {
  const res = await fetch(`${BASE}/api/scheduled-tasks/preview`, {
    method: 'POST',
    headers: JSON_HEADERS,
    body: JSON.stringify(input),
  })
  if (!res.ok) throw new Error(await errorText(res, 'Failed to preview schedule'))
  return res.json()
}

export async function fetchTelegramChats(): Promise<TelegramChatTarget[]> {
  const res = await fetch(`${BASE}/api/scheduled-tasks/telegram-chats`)
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

async function errorText(res: Response, fallback: string): Promise<string> {
  try {
    const payload = await res.json()
    return String(payload.detail || fallback)
  } catch {
    return fallback
  }
}
