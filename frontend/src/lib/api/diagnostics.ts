import { BASE } from './client'
import type {
  BrowserUseDiagnostics,
  DiagnosticsSummary,
  EvaluationReplayResult,
  EvaluationRun,
  MCPServerDiagnostics,
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

export async function fetchEvaluationRuns(limit = 50): Promise<EvaluationRun[]> {
  const params = new URLSearchParams({ limit: String(limit) })
  const res = await fetch(`${BASE}/api/diagnostics/evaluations?${params}`)
  if (!res.ok) throw new Error('Failed to fetch evaluation runs')
  return res.json()
}

export async function replayEvaluationRun(runId: string): Promise<EvaluationReplayResult> {
  const res = await fetch(`${BASE}/api/diagnostics/evaluations/${encodeURIComponent(runId)}/replay`, {
    method: 'POST',
  })
  if (!res.ok) throw new Error('Failed to replay evaluation run')
  return res.json()
}
