import { apiFetch, BASE, JSON_HEADERS } from './client'
import type { AccessGrantDecision, AccessGrantTicket } from './types'

export async function fetchPendingAccessGrants(
  conversationId?: string,
): Promise<AccessGrantTicket[]> {
  const qs = conversationId
    ? `?conversation_id=${encodeURIComponent(conversationId)}`
    : ''
  const res = await apiFetch(`${BASE}/api/access-grants/pending${qs}`)
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
