import { apiFetch, BASE, JSON_HEADERS } from './client'
import type { AccessGrantDecision, AccessGrantTicket } from './types'

export async function fetchPendingAccessGrants(
  conversationId?: string,
  limit = 50,
  offset = 0,
): Promise<AccessGrantTicket[]> {
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) })
  if (conversationId) params.set('conversation_id', conversationId)
  const res = await apiFetch(`${BASE}/api/access-grants/pending?${params}`)
  if (!res.ok) throw new Error('Failed to fetch pending access grants')
  return res.json()
}

export async function resolveAccessGrant(
  ticketId: string,
  decision: AccessGrantDecision,
): Promise<AccessGrantTicket> {
  const res = await apiFetch(`${BASE}/api/access-grants/${ticketId}/resolve`, {
    method: 'POST',
    headers: JSON_HEADERS,
    body: JSON.stringify({ decision }),
  })
  if (!res.ok) throw new Error('Failed to resolve access grant')
  return res.json()
}
