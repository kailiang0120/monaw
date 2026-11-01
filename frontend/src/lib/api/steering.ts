import { apiFetch, BASE, JSON_HEADERS, throwApiError } from './client'
import type { ChatRun, ChatSteerResponse, UploadedAttachment } from './types'

/** Turns this window started that are still running, including ones in chats it left. */
export async function fetchChatRuns(signal?: AbortSignal): Promise<ChatRun[]> {
  const response = await apiFetch(`${BASE}/api/chat/runs`, { signal })
  if (!response.ok) await throwApiError(response, 'Could not list running turns')
  return response.json()
}

export async function sendSteeringMessage(
  runId: string,
  messageId: string,
  message: string,
  attachments: UploadedAttachment[],
): Promise<ChatSteerResponse> {
  const response = await apiFetch(`${BASE}/api/chat/steer`, {
    method: 'POST',
    headers: JSON_HEADERS,
    body: JSON.stringify({ run_id: runId, message_id: messageId, message, attachments }),
  })
  if (!response.ok) await throwApiError(response, 'Could not deliver the steering message')
  return response.json()
}

/** Ends a running turn. Closing its stream only stops watching it. */
export async function stopChatRun(runId: string): Promise<boolean> {
  const response = await apiFetch(`${BASE}/api/chat/stop`, {
    method: 'POST',
    headers: JSON_HEADERS,
    body: JSON.stringify({ run_id: runId }),
  })
  if (!response.ok) await throwApiError(response, 'Could not stop the turn')
  return Boolean((await response.json()).ok)
}

/** A run ID chosen before sending, so Stop works before the server replies. */
export function newRunId(): string {
  return globalThis.crypto?.randomUUID?.() ?? `run-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`
}
