import { apiFetch, BASE, JSON_HEADERS } from './client'
import type {
  MemoryAuditRecord,
  MemoryCandidate,
  MemoryCategory,
  MemoryCheckpoint,
  MemoryEpisode,
  MemoryFileRecord,
  MemoryKind,
  MemoryProfileField,
  MemoryRecord,
  MemoryReviewState,
  MemorySearchRecord,
  MemoryStats,
  MemoryStatus,
} from './types'

export interface MemoryListParams {
  query?: string
  category?: '' | MemoryCategory
  status?: '' | MemoryStatus
  review_state?: '' | MemoryReviewState
  limit?: number
}

function paramsToSearch(params: object = {}) {
  const search = new URLSearchParams()
  Object.entries(params as Record<string, string | number | boolean | null | undefined>).forEach(([key, value]) => {
    if (value === undefined || value === null || value === '') return
    search.set(key, String(value))
  })
  return search.toString()
}

async function errorMessage(res: Response, fallback: string) {
  const payload = await res.json().catch(() => null)
  const detail = payload?.detail
  if (typeof detail === 'string' && detail.trim()) return detail
  if (Array.isArray(detail) && detail.length > 0) {
    return detail
      .map((item) => item?.msg || item?.message || '')
      .filter(Boolean)
      .join('; ') || fallback
  }
  return fallback
}

export async function fetchMemories(params: MemoryListParams = {}): Promise<MemoryRecord[]> {
  const query = paramsToSearch(params)
  const res = await apiFetch(`${BASE}/api/memories${query ? `?${query}` : ''}`)
  if (!res.ok) throw new Error(await errorMessage(res, 'Failed to fetch memories'))
  return res.json()
}

export async function fetchMemoryStats(): Promise<MemoryStats> {
  const res = await apiFetch(`${BASE}/api/memories/stats`)
  if (!res.ok) throw new Error(await errorMessage(res, 'Failed to fetch memory stats'))
  return res.json()
}

export async function fetchMemoryFile(category: MemoryCategory): Promise<MemoryFileRecord> {
  const res = await apiFetch(`${BASE}/api/memories/files/${category}`)
  if (!res.ok) throw new Error(await errorMessage(res, 'Failed to fetch memory file'))
  return res.json()
}

export async function saveMemoryFile(category: MemoryCategory, raw_markdown: string): Promise<MemoryFileRecord> {
  const res = await apiFetch(`${BASE}/api/memories/files/${category}`, {
    method: 'PUT',
    headers: JSON_HEADERS,
    body: JSON.stringify({ raw_markdown }),
  })
  if (!res.ok) throw new Error(await errorMessage(res, 'Failed to save memory file'))
  return res.json()
}

export async function updateSection(
  sectionId: string,
  body: Partial<Pick<MemoryFileRecord['sections'][number], 'title' | 'body' | 'importance' | 'review_state'>>,
): Promise<MemoryFileRecord['sections'][number]> {
  const res = await apiFetch(`${BASE}/api/memories/sections/${sectionId}`, {
    method: 'PATCH',
    headers: JSON_HEADERS,
    body: JSON.stringify(body),
  })
  if (!res.ok) throw new Error(await errorMessage(res, 'Failed to update memory section'))
  return res.json()
}

export async function searchMemories(params: {
  query: string
  category?: '' | MemoryCategory
  include_archived?: boolean
  review_state?: '' | MemoryReviewState
  limit?: number
}): Promise<MemorySearchRecord[]> {
  const query = paramsToSearch(params)
  const res = await apiFetch(`${BASE}/api/memories/search?${query}`)
  if (!res.ok) throw new Error(await errorMessage(res, 'Failed to search memories'))
  return res.json()
}

export async function fetchMemoryAudit(params: {
  conversation_id?: string
  memory_id?: string
  limit?: number
} = {}): Promise<MemoryAuditRecord[]> {
  const query = paramsToSearch(params)
  const res = await apiFetch(`${BASE}/api/memories/audit${query ? `?${query}` : ''}`)
  if (!res.ok) throw new Error(await errorMessage(res, 'Failed to fetch memory audit'))
  return res.json()
}

export async function closeMemorySession(conversation_id: string): Promise<{
  conversation_id: string
  reconciliation: Record<string, number>
  archived_messages: number
  maintenance: Record<string, number>
  hot_messages_before: number
  hot_messages_after: number
  reflection_id: string | null
}> {
  const res = await apiFetch(`${BASE}/api/memories/session/close`, {
    method: 'POST',
    headers: JSON_HEADERS,
    body: JSON.stringify({ conversation_id }),
  })
  if (!res.ok) throw new Error(await errorMessage(res, 'Failed to close memory session'))
  return res.json()
}

export async function fetchMemoryProfile(): Promise<MemoryProfileField[]> {
  const res = await apiFetch(`${BASE}/api/memories/profile`)
  if (!res.ok) throw new Error(await errorMessage(res, 'Failed to fetch memory profile'))
  return res.json()
}

export async function updateMemoryProfileField(
  field: string,
  body: Pick<MemoryProfileField, 'value' | 'privacy_level' | 'confidence' | 'review_state'> & {
    source_conversation_id?: string
    source_message_id?: number | null
  },
): Promise<MemoryProfileField> {
  const res = await apiFetch(`${BASE}/api/memories/profile/${encodeURIComponent(field)}`, {
    method: 'PATCH',
    headers: JSON_HEADERS,
    body: JSON.stringify(body),
  })
  if (!res.ok) throw new Error(await errorMessage(res, 'Failed to update memory profile'))
  return res.json()
}

export async function fetchMemoryCandidates(params: {
  status?: '' | MemoryCandidate['status']
  limit?: number
} = {}): Promise<MemoryCandidate[]> {
  const query = paramsToSearch(params)
  const res = await apiFetch(`${BASE}/api/memories/candidates${query ? `?${query}` : ''}`)
  if (!res.ok) throw new Error(await errorMessage(res, 'Failed to fetch memory candidates'))
  return res.json()
}

export async function updateMemoryCandidate(
  id: string,
  body: { status: MemoryCandidate['status']; approve?: boolean },
): Promise<MemoryCandidate> {
  const res = await apiFetch(`${BASE}/api/memories/candidates/${encodeURIComponent(id)}`, {
    method: 'PATCH',
    headers: JSON_HEADERS,
    body: JSON.stringify(body),
  })
  if (!res.ok) throw new Error(await errorMessage(res, 'Failed to update memory candidate'))
  return res.json()
}

export async function fetchMemoryEpisodes(params: {
  conversation_id?: string
  limit?: number
} = {}): Promise<MemoryEpisode[]> {
  const query = paramsToSearch(params)
  const res = await apiFetch(`${BASE}/api/memories/episodes${query ? `?${query}` : ''}`)
  if (!res.ok) throw new Error(await errorMessage(res, 'Failed to fetch memory episodes'))
  return res.json()
}

export async function fetchMemoryCheckpoints(params: {
  status?: '' | MemoryCheckpoint['status']
  limit?: number
} = {}): Promise<MemoryCheckpoint[]> {
  const query = paramsToSearch(params)
  const res = await apiFetch(`${BASE}/api/memories/checkpoints${query ? `?${query}` : ''}`)
  if (!res.ok) throw new Error(await errorMessage(res, 'Failed to fetch memory checkpoints'))
  return res.json()
}

export async function createMemory(body: {
  content: string
  category: MemoryCategory
  review_state?: MemoryReviewState
  confidence?: number
  importance?: number
  kind?: MemoryKind
}): Promise<MemoryRecord> {
  const res = await apiFetch(`${BASE}/api/memories`, {
    method: 'POST',
    headers: JSON_HEADERS,
    body: JSON.stringify(body),
  })
  if (!res.ok) throw new Error(await errorMessage(res, 'Failed to create memory'))
  return res.json()
}

export async function updateMemory(
  id: string,
  body: Partial<Pick<MemoryRecord, 'content' | 'category' | 'status' | 'review_state' | 'confidence' | 'importance' | 'kind'>>,
): Promise<MemoryRecord> {
  const res = await apiFetch(`${BASE}/api/memories/${id}`, {
    method: 'PATCH',
    headers: JSON_HEADERS,
    body: JSON.stringify(body),
  })
  if (!res.ok) throw new Error(await errorMessage(res, 'Failed to update memory'))
  return res.json()
}

export async function deleteMemory(id: string): Promise<void> {
  const res = await apiFetch(`${BASE}/api/memories/${id}`, { method: 'DELETE' })
  if (!res.ok) throw new Error(await errorMessage(res, 'Failed to delete memory'))
}
