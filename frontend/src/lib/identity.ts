export const DEFAULT_AGENT_NAME = 'Monaw'
export const LEGACY_AGENT_NAME = 'Agent'

export function resolveAgentName(value?: string | null): string {
  const trimmed = (value ?? '').trim()
  return !trimmed || trimmed === LEGACY_AGENT_NAME ? DEFAULT_AGENT_NAME : trimmed
}
