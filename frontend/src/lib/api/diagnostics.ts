import { BASE } from './client'
import type {
  BrowserUseDiagnostics,
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

export async function fetchDiagnosticsSummary(): Promise<DiagnosticsSummary> {
  const res = await fetch(`${BASE}/api/diagnostics/summary`)
  if (!res.ok) throw new Error('Failed to fetch diagnostics summary')
  return res.json()
}

export async function fetchMCPDiagnostics(): Promise<MCPServerDiagnostics[]> {
  const res = await fetch(`${BASE}/api/diagnostics/mcp`)
  if (!res.ok) throw new Error('Failed to fetch MCP diagnostics')
  return res.json()
}

export async function fetchBrowserUseDiagnostics(): Promise<BrowserUseDiagnostics> {
  const res = await fetch(`${BASE}/api/diagnostics/browser-use`)
  if (!res.ok) throw new Error('Failed to fetch browser-use diagnostics')
  return res.json()
}

export async function resetBrowserUseSession(): Promise<BrowserUseDiagnostics> {
  const res = await fetch(`${BASE}/api/diagnostics/browser-use/reset`, {
    method: 'POST',
  })
  if (!res.ok) throw new Error('Failed to reset browser-use session')
  return res.json()
}

export async function reconnectMCPServer(name: string): Promise<MCPServerDiagnostics> {
  const res = await fetch(`${BASE}/api/diagnostics/mcp/${encodeURIComponent(name)}/reconnect`, {
    method: 'POST',
  })
  if (!res.ok) throw new Error('Failed to reconnect MCP server')
  return res.json()
}

export async function fetchObservabilitySummary(): Promise<ObservabilitySummary> {
  const res = await fetch(`${BASE}/api/observability/summary`)
  if (!res.ok) throw new Error('Failed to fetch observability summary')
  return res.json()
}

export async function fetchObservabilityRuns(filters: {
  status?: string
  source?: string
  model?: string
  q?: string
  limit?: number
} = {}): Promise<ObservabilityRun[]> {
  const params = new URLSearchParams()
  if (filters.status) params.set('status', filters.status)
  if (filters.source) params.set('source', filters.source)
  if (filters.model) params.set('model', filters.model)
  if (filters.q) params.set('q', filters.q)
  params.set('limit', String(filters.limit ?? 100))
  const res = await fetch(`${BASE}/api/observability/runs?${params}`)
  if (!res.ok) throw new Error('Failed to fetch observability runs')
  return res.json()
}

export async function fetchObservabilityRun(runId: string): Promise<ObservabilityRunDetail> {
  const res = await fetch(`${BASE}/api/observability/runs/${encodeURIComponent(runId)}`)
  if (!res.ok) throw new Error('Failed to fetch observability run')
  return res.json()
}

export async function fetchObservabilityErrors(filters: {
  level?: string
  q?: string
  limit?: number
} = {}): Promise<ObservabilityError[]> {
  const params = new URLSearchParams()
  if (filters.level) params.set('level', filters.level)
  if (filters.q) params.set('q', filters.q)
  params.set('limit', String(filters.limit ?? 100))
  const res = await fetch(`${BASE}/api/observability/errors?${params}`)
  if (!res.ok) throw new Error('Failed to fetch observability errors')
  return res.json()
}

export async function fetchBackendLogTail(tail = 400): Promise<ObservabilityBackendLog> {
  const params = new URLSearchParams({ tail: String(tail) })
  const res = await fetch(`${BASE}/api/observability/logs/backend?${params}`)
  if (!res.ok) throw new Error('Failed to fetch backend log')
  return res.json()
}

export async function replayObservabilityRun(runId: string): Promise<ObservabilityReplayResult> {
  const res = await fetch(`${BASE}/api/observability/runs/${encodeURIComponent(runId)}/replay`, {
    method: 'POST',
  })
  if (!res.ok) throw new Error('Failed to replay observability run')
  return res.json()
}

export async function exportObservabilityDebugBundle(runId: string): Promise<ObservabilityDebugBundle> {
  const res = await fetch(`${BASE}/api/observability/runs/${encodeURIComponent(runId)}/export-debug-bundle`, {
    method: 'POST',
  })
  if (!res.ok) throw new Error('Failed to export debug bundle')
  return res.json()
}
