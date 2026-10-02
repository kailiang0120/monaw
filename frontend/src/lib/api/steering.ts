import { apiFetch, BASE, JSON_HEADERS, throwApiError } from './client'
import type { ChatSteerResponse, UploadedAttachment } from './types'

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
