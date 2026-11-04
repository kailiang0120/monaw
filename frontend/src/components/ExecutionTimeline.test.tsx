import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { RunOperation, RunSnapshot } from '../lib/api/runs'
import { ExecutionTimeline } from './ExecutionTimeline'

const runningSnapshot: RunSnapshot = {
  run_id: 'run-ui',
  conversation_id: 'chat-ui',
  attempt: 2,
  revision: 9,
  status: 'running',
  reason_code: null,
  available_actions: ['pause', 'stop'],
  last_seq: 16,
  created_at: '2026-11-03T01:00:00Z',
  updated_at: '2026-11-03T01:05:00Z',
  finished_at: null,
}

const verifiedOperation: RunOperation = {
  run_id: runningSnapshot.run_id,
  operation_id: 'op-file-write',
  sequence: 2,
  attempt: 2,
  operation_kind: 'file_write',
  related_operation_id: null,
  effect_class: 'verifiable_write',
  status: 'verified',
  reason_code: null,
  input_summary: { path: 'notes.txt' },
  evidence: { verification_result: 'completed', sha256_matches: true },
  snapshot_available: true,
  created_at: '2026-11-03T01:01:00Z',
  updated_at: '2026-11-03T01:02:00Z',
}

describe('ExecutionTimeline', () => {
  afterEach(cleanup)

  it('uses server actions for controls and exposes them as keyboard-focusable buttons', () => {
    render(<ExecutionTimeline snapshot={runningSnapshot} onAction={vi.fn()} />)

    const pause = screen.getByRole('button', { name: 'Pause execution' })
    const stop = screen.getByRole('button', { name: 'Stop execution' })
    expect(pause.tagName).toBe('BUTTON')
    expect(stop.tagName).toBe('BUTTON')
    expect(pause).toBeEnabled()
    expect(screen.queryByRole('button', { name: 'Continue execution' })).not.toBeInTheDocument()

    pause.focus()
    expect(document.activeElement).toBe(pause)
  })

  it('sends at most one action when the user double-clicks while the request is pending', async () => {
    let settleAction!: () => void
    const onAction = vi.fn(() => new Promise<void>((resolve) => { settleAction = resolve }))
    render(<ExecutionTimeline snapshot={runningSnapshot} onAction={onAction} />)

    const pause = screen.getByRole('button', { name: 'Pause execution' })
    fireEvent.click(pause)
    fireEvent.click(pause)

    expect(onAction).toHaveBeenCalledTimes(1)
    expect(pause).toBeDisabled()
    await act(async () => { settleAction() })
    expect(pause).toBeEnabled()
  })

  it('shows the recovery status and evidence from the server operation record', () => {
    const snapshot: RunSnapshot = {
      ...runningSnapshot,
      status: 'needs_review',
      reason_code: 'external_effect_unknown',
      available_actions: ['inspect', 'verify', 'stop'],
    }
    render(
      <ExecutionTimeline
        snapshot={snapshot}
        operations={[{ ...verifiedOperation, status: 'needs_review', evidence: { verification_result: 'unknown' } }]}
        recovery={{
          run_id: snapshot.run_id,
          run: snapshot,
          unresolved_operations: [{ ...verifiedOperation, status: 'needs_review', evidence: { verification_result: 'unknown' } }],
          operations_next_sequence: 2,
          operations_truncated: false,
        }}
        onResolveOperation={vi.fn()}
      />,
    )

    expect(screen.getAllByText('Needs review', { exact: true })).toHaveLength(2)
    expect(screen.getByText('An action may have taken effect, but its result could not be confirmed.')).toBeInTheDocument()
    fireEvent.click(screen.getByText(/Operation details/))
    fireEvent.click(screen.getByText('Verification evidence'))
    expect(screen.getByText('unknown')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Verify' })).toBeInTheDocument()
  })
})
