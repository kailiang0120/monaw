export interface BrowserActionOutcome {
  status: 'executed' | 'verified' | 'failed' | 'uncertain' | 'needs_review'
  action_executed?: boolean
  action_id?: string
  verification?: { type?: string; passed?: boolean; reason?: string }
  recovery?: { reason_code?: string; retry_allowed?: boolean }
}

export function browserOperationOutcome(evidence: Record<string, string | number | boolean>): BrowserActionOutcome | undefined {
  const status = evidence.browser_status
  if (typeof status !== 'string' || !['executed', 'verified', 'failed', 'uncertain', 'needs_review'].includes(status)) return undefined
  return { status: status as BrowserActionOutcome['status'],
    recovery: typeof evidence.browser_reason_code === 'string' ? { reason_code: evidence.browser_reason_code } : undefined }
}

/** Only the bundled browser adapter supplies browser verification evidence. */
export function browserActionOutcome(tool: string, output?: string): BrowserActionOutcome | undefined {
  if (!tool.startsWith('mcp__chrome-devtools__')) return undefined
  if (!output || output.length > 2_000_000) return undefined
  try {
    const parsed = JSON.parse(output)
    const candidate = parsed.action_outcome || parsed.structured_content?.action_outcome || parsed.structuredContent?.action_outcome
    if (!candidate || !['executed', 'verified', 'failed', 'uncertain', 'needs_review'].includes(candidate.status)) return undefined
    return candidate as BrowserActionOutcome
  } catch { return undefined }
}
