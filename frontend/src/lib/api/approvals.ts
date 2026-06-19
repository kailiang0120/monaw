import { apiFetch, BASE, JSON_HEADERS } from './client'
import type { ApprovalTicket } from './types'

export async function fetchPendingApprovals(conversationId?: string): Promise<ApprovalTicket[]> {
  const qs = conversationId ? `?conversation_id=${encodeURIComponent(conversationId)}` : ''
  const res = await apiFetch(`${BASE}/api/approvals/pending${qs}`)
  if (!res.ok) throw new Error('Failed to fetch pending approvals')
  return res.json()
}

export async function approveTicket(ticketId: string): Promise<ApprovalTicket> {
  const res = await apiFetch(`${BASE}/api/approvals/${ticketId}/approve`, {
    method: 'POST',
    headers: JSON_HEADERS,
    body: JSON.stringify({ resolved_by: 'user' }),
  })
  if (!res.ok) throw new Error('Failed to approve ticket')
  return res.json()
}

export async function rejectTicket(ticketId: string): Promise<ApprovalTicket> {
  const res = await apiFetch(`${BASE}/api/approvals/${ticketId}/reject`, {
    method: 'POST',
    headers: JSON_HEADERS,
    body: JSON.stringify({ resolved_by: 'user' }),
  })
  if (!res.ok) throw new Error('Failed to reject ticket')
  return res.json()
}

export async function fetchApprovalHistory(conversationId?: string, limit = 50): Promise<ApprovalTicket[]> {
  const params = new URLSearchParams()
  if (conversationId) params.set('conversation_id', conversationId)
  params.set('limit', String(limit))
  const res = await apiFetch(`${BASE}/api/approvals/history?${params}`)
  if (!res.ok) throw new Error('Failed to fetch approval history')
  return res.json()
}
