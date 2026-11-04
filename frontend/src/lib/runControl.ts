import type { RunSnapshot } from './api/runs'

export type ComposerRunControl = 'pause' | 'resume' | 'pausing'

/**
 * The composer's Pause/Continue control. While a turn streams, only that turn's
 * Run counts: the selected snapshot can still be the previous Run (a new turn
 * waits for the one before it), and pausing that one would stall the new message.
 */
export function composerRunControl(
  snapshot: RunSnapshot | null,
  isStreaming: boolean,
  streamingRunId: string | null,
): ComposerRunControl | undefined {
  if (!snapshot) return undefined
  if (isStreaming) {
    if (!streamingRunId || snapshot.run_id !== streamingRunId) return undefined
    if (snapshot.status === 'pausing') return 'pausing'
    return snapshot.available_actions?.includes('pause') ? 'pause' : undefined
  }
  if (snapshot.status === 'pausing') return 'pausing'
  return snapshot.available_actions?.includes('resume') ? 'resume' : undefined
}
