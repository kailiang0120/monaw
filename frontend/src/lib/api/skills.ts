import { apiFetch, BASE, JSON_HEADERS, throwApiError } from './client'
import type { AgentSettings } from './types'

export interface SkillImportPreview {
  token: string
  skills: Array<{ id: string; name: string; description: string; files: number; installed: boolean }>
}

/** Inspect a local path, public GitHub source, or uploaded skill archive. */
export async function inspectSkills(source: { source?: string; filename?: string; data_base64?: string }): Promise<SkillImportPreview> {
  const response = await apiFetch(`${BASE}/api/skills/inspect`, {
    method: 'POST', headers: JSON_HEADERS, body: JSON.stringify(source),
  })
  if (!response.ok) await throwApiError(response, 'Could not inspect skills')
  return response.json()
}

/** Install the exact skills selected from a session-bound preview. */
export async function installSkills(token: string, skills: string[]): Promise<{ installed: string[]; settings: AgentSettings }> {
  const response = await apiFetch(`${BASE}/api/skills/install`, {
    method: 'POST', headers: JSON_HEADERS, body: JSON.stringify({ token, skills }),
  })
  if (!response.ok) await throwApiError(response, 'Could not install skills')
  return response.json()
}
