import { describe, expect, it } from 'vitest'
import type { RunSnapshot } from './api/runs'
import { composerRunControl } from './runControl'

const run = (overrides: Partial<RunSnapshot>): RunSnapshot => ({
  run_id: 'run-1', status: 'running', available_actions: ['pause'], ...overrides,
} as RunSnapshot)

describe('composerRunControl', () => {
  it('pauses only the Run being streamed', () => {
    expect(composerRunControl(run({}), true, 'run-1')).toBe('pause')
    // A new message waits for the previous Run; its snapshot must not be paused instead.
    expect(composerRunControl(run({ run_id: 'old-run' }), true, 'run-2')).toBeUndefined()
    expect(composerRunControl(run({}), true, null)).toBeUndefined()
  })

  it('offers Continue only when nothing is streaming', () => {
    const paused = run({ status: 'paused', available_actions: ['resume'] })
    expect(composerRunControl(paused, false, null)).toBe('resume')
    expect(composerRunControl(paused, true, 'run-2')).toBeUndefined()
  })

  it('shows pausing for the selected Run', () => {
    const pausing = run({ status: 'pausing', available_actions: [] })
    expect(composerRunControl(pausing, true, 'run-1')).toBe('pausing')
    expect(composerRunControl(pausing, false, null)).toBe('pausing')
    expect(composerRunControl(null, true, 'run-1')).toBeUndefined()
  })
})
