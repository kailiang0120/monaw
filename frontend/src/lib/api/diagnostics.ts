import { apiFetch, BASE, JSON_HEADERS } from './client'
import type {
  BrowserUseDiagnostics,
  DataDeletionResult,
  DiagnosticsSummary,
  MCPServerDiagnostics,
  ObservabilityBackendLog,
  ObservabilityDebugBundle,
  ObservabilityError,
  ObservabilityReplayResult,
  ObservabilityRun,
  ObservabilityRunDetail,
  ObservabilitySummary,
} from './types'

export async function fetchDiagnosticsSummary(signal?: AbortSignal): Promise<DiagnosticsSummary> {
  const res = await apiFetch(`${BASE}/api/diagnostics/summary`, { signal })
  if (!res.ok) throw new Error('Failed to fetch diagnostics summary')
  return res.json()
}

export async function fetchMCPDiagnostics(signal?: AbortSignal): Promise<MCPServerDiagnostics[]> {
  const res = await apiFetch(`${BASE}/api/diagnostics/mcp`, { signal })
  if (!res.ok) throw new Error('Failed to fetch MCP diagnostics')
  return res.json()
}

export async function fetchBrowserUseDiagnostics(signal?: AbortSignal): Promise<BrowserUseDiagnostics> {
  const res = await apiFetch(`${BASE}/api/diagnostics/browser-use`, { signal })
  if (!res.ok) throw new Error('Failed to fetch browser-use diagnostics')
  return res.json()
}

export async function resetBrowserUseSession(): Promise<BrowserUseDiagnostics> {
  const res = await apiFetch(`${BASE}/api/diagnostics/browser-use/reset`, {
    method: 'POST',
  })
  if (!res.ok) throw new Error('Failed to reset browser-use session')
  return res.json()
}

export async function reconnectMCPServer(name: string): Promise<MCPServerDiagnostics> {
  const res = await apiFetch(`${BASE}/api/diagnostics/mcp/${encodeURIComponent(name)}/reconnect`, {
    method: 'POST',
  })
  if (!res.ok) throw new Error('Failed to reconnect MCP server')
  return res.json()
}

export async function fetchObservabilitySummary(signal?: AbortSignal): Promise<ObservabilitySummary> {
  const res = await apiFetch(`${BASE}/api/observability/summary`, { signal })
  if (!res.ok) throw new Error('Failed to fetch observability summary')
  return res.json()
}

export async function fetchObservabilityRuns(filters: {
  status?: string
  source?: string
  model?: string
  q?: string
  limit?: number
  signal?: AbortSignal
} = {}): Promise<ObservabilityRun[]> {
  const params = new URLSearchParams()
  if (filters.status) params.set('status', filters.status)
  if (filters.source) params.set('source', filters.source)
  if (filters.model) params.set('model', filters.model)
  if (filters.q) params.set('q', filters.q)
  params.set('limit', String(filters.limit ?? 100))
  const res = await apiFetch(`${BASE}/api/observability/runs?${params}`, { signal: filters.signal })
  if (!res.ok) throw new Error('Failed to fetch observability runs')
  return res.json()
}

export async function fetchObservabilityRun(
  runId: string,
  signal?: AbortSignal,
): Promise<ObservabilityRunDetail> {
  const res = await apiFetch(`${BASE}/api/observability/runs/${encodeURIComponent(runId)}`, { signal })
  if (!res.ok) throw new Error('Failed to fetch observability run')
  return res.json()
}

export async function fetchObservabilityErrors(filters: {
  level?: string
  q?: string
  limit?: number
  signal?: AbortSignal
} = {}): Promise<ObservabilityError[]> {
  const params = new URLSearchParams()
  if (filters.level) params.set('level', filters.level)
  if (filters.q) params.set('q', filters.q)
  params.set('limit', String(filters.limit ?? 100))
  const res = await apiFetch(`${BASE}/api/observability/errors?${params}`, { signal: filters.signal })
  if (!res.ok) throw new Error('Failed to fetch observability errors')
  return res.json()
}

export async function fetchBackendLogTail(tail = 400, signal?: AbortSignal): Promise<ObservabilityBackendLog> {
  const params = new URLSearchParams({ tail: String(tail) })
  const res = await apiFetch(`${BASE}/api/observability/logs/backend?${params}`, { signal })
  if (!res.ok) throw new Error('Failed to fetch backend log')
  return res.json()
}

export async function replayObservabilityRun(runId: string): Promise<ObservabilityReplayResult> {
  const res = await apiFetch(`${BASE}/api/observability/runs/${encodeURIComponent(runId)}/replay`, {
    method: 'POST',
  })
  if (!res.ok) throw new Error('Failed to replay observability run')
  return res.json()
}

export async function exportObservabilityDebugBundle(runId: string): Promise<ObservabilityDebugBundle> {
  const res = await apiFetch(`${BASE}/api/observability/runs/${encodeURIComponent(runId)}/export-debug-bundle`, {
    method: 'POST',
  })
  if (!res.ok) throw new Error('Failed to export debug bundle')
  return res.json()
}

export async function deleteRuntimeData(): Promise<DataDeletionResult> {
  const res = await apiFetch(`${BASE}/api/privacy/delete-data`, {
    method: 'POST',
    headers: JSON_HEADERS,
    body: JSON.stringify({ confirm: true }),
  })
  if (!res.ok) throw new Error('Failed to delete runtime data')
  return res.json()
}
