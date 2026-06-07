import { BASE, JSON_HEADERS } from './client'
import type { ContextUsage, Conversation, MessagesResponse, SavedMessage } from './types'

export async function fetchConversations(): Promise<Conversation[]> {
  const res = await fetch(`${BASE}/api/conversations`)
  if (!res.ok) throw new Error('Failed to fetch conversations')
  return res.json()
}

export async function createConversation(title?: string): Promise<Conversation> {
  const res = await fetch(`${BASE}/api/conversations`, {
    method: 'POST',
    headers: JSON_HEADERS,
    body: JSON.stringify({ title: title ?? 'New Conversation' }),
  })
  if (!res.ok) throw new Error('Failed to create conversation')
  return res.json()
}

export async function deleteConversation(id: string): Promise<void> {
  await fetch(`${BASE}/api/conversations/${id}`, { method: 'DELETE' })
}

export async function renameConversation(id: string, title: string): Promise<Conversation> {
  const res = await fetch(`${BASE}/api/conversations/${encodeURIComponent(id)}`, {
    method: 'PATCH',
    headers: JSON_HEADERS,
    body: JSON.stringify({ title }),
  })
  if (!res.ok) throw new Error('Failed to rename conversation')
  return res.json()
}

export async function fetchContextUsage(conversationId: string): Promise<ContextUsage> {
  const res = await fetch(`${BASE}/api/conversations/${encodeURIComponent(conversationId)}/context-usage`)
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
  const res = await fetch(
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
  const res = await fetch(
    `${BASE}/api/messages/${encodeURIComponent(String(messageId))}/tool-calls`,
    { signal },
  )
  if (!res.ok) throw new Error('Failed to fetch message tool calls')
  return res.json()
}
