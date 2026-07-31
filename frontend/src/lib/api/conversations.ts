import { apiFetch, BASE, JSON_HEADERS } from './client'
import type { ContextUsage, Conversation, MessagesResponse, SavedMessage } from './types'

export async function fetchConversations(limit = 100, offset = 0): Promise<Conversation[]> {
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) })
  const res = await apiFetch(`${BASE}/api/conversations?${params}`)
  if (!res.ok) throw new Error('Failed to fetch conversations')
  return res.json()
}

export async function createConversation(title?: string): Promise<Conversation> {
  const res = await apiFetch(`${BASE}/api/conversations`, {
    method: 'POST',
    headers: JSON_HEADERS,
    body: JSON.stringify({ title: title ?? 'New Conversation' }),
  })
  if (!res.ok) throw new Error('Failed to create conversation')
  return res.json()
}

export async function deleteConversation(id: string): Promise<void> {
  await apiFetch(`${BASE}/api/conversations/${id}`, { method: 'DELETE' })
}

export async function renameConversation(id: string, title: string): Promise<Conversation> {
  const res = await apiFetch(`${BASE}/api/conversations/${encodeURIComponent(id)}`, {
    method: 'PATCH',
    headers: JSON_HEADERS,
    body: JSON.stringify({ title }),
  })
  if (!res.ok) throw new Error('Failed to rename conversation')
  return res.json()
}

export async function fetchContextUsage(conversationId: string, signal?: AbortSignal): Promise<ContextUsage> {
  const res = await apiFetch(
    `${BASE}/api/conversations/${encodeURIComponent(conversationId)}/context-usage`,
    { signal },
  )
  if (!res.ok) throw new Error('Failed to fetch context usage')
  return res.json()
}

export async function fetchMessages(
  conversationId: string,
  limit = 50,
  beforeId?: number,
  signal?: AbortSignal,
): Promise<MessagesResponse> {
  const params = new URLSearchParams({ limit: String(limit) })
  if (beforeId !== undefined) params.set('before_id', String(beforeId))
  params.set('tool_call_mode', 'summary')
  const res = await apiFetch(
    `${BASE}/api/conversations/${encodeURIComponent(conversationId)}/messages?${params}`,
    { signal },
  )
  if (!res.ok) throw new Error('Failed to fetch messages')
  return res.json()
}

export async function fetchMessageToolCalls(
  messageId: number,
  signal?: AbortSignal,
): Promise<SavedMessage['tool_calls']> {
  const params = new URLSearchParams({
    limit: '100',
    offset: '0',
    tool_payload_limit: '20000',
  })
  const res = await apiFetch(
    `${BASE}/api/messages/${encodeURIComponent(String(messageId))}/tool-calls?${params}`,
    { signal },
  )
  if (!res.ok) throw new Error('Failed to fetch message tool calls')
  return res.json()
}
