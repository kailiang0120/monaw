import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { RunEvent, RunOperation, RunSnapshot } from '../lib/api/runs'
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

const runEvent: RunEvent = {
  run_id: runningSnapshot.run_id,
  seq: 14,
  type: 'operation.verified',
  at: '2026-11-03T01:02:00Z',
  payload: { private_text: 'Never show arbitrary event payloads.' },
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

  it('keeps concise details temporary and preserves an expanded panel and controls when the mode changes', () => {
    const { rerender } = render(
      <ExecutionTimeline
        snapshot={runningSnapshot}
        operations={[verifiedOperation]}
        detailMode="simple"
        onAction={vi.fn()}
      />,
    )

    expect(screen.queryByText('Run ID')).not.toBeInTheDocument()
    const operationsDetails = screen.getByText(/Operation details/).closest('details')
    expect(operationsDetails).not.toHaveAttribute('open')
    fireEvent.click(screen.getByText(/Operation details/))
    expect(operationsDetails).toHaveAttribute('open')

    rerender(
      <ExecutionTimeline
        snapshot={runningSnapshot}
        operations={[verifiedOperation]}
        events={[runEvent]}
        detailMode="developer"
        onAction={vi.fn()}
      />,
    )

    expect(operationsDetails).toHaveAttribute('open')
    expect(screen.getByText(runningSnapshot.run_id)).toBeInTheDocument()
    expect(screen.getByText('op-file-write')).toBeInTheDocument()
    expect(screen.getByText(/#14 operation.verified/)).toBeInTheDocument()
    expect(screen.queryByText('Never show arbitrary event payloads.')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Pause execution' })).toBeEnabled()
    expect(screen.getByRole('button', { name: 'Stop execution' })).toBeEnabled()
  })

  it('auto-expands steps in detail modes while allowing manual collapse and preserving it when returning to concise', () => {
    const { rerender } = render(
      <ExecutionTimeline snapshot={runningSnapshot} operations={[verifiedOperation]} detailMode="simple" />,
    )
    const details = screen.getByText(/Operation details/).closest('details')
    expect(details).not.toHaveAttribute('open')

    rerender(<ExecutionTimeline snapshot={runningSnapshot} operations={[verifiedOperation]} detailMode="detailed" />)
    expect(details).toHaveAttribute('open')
    expect(screen.getByText(/2\. File write/)).toBeInTheDocument()
    fireEvent.click(screen.getByText(/Operation details/))
    expect(details).not.toHaveAttribute('open')

    rerender(<ExecutionTimeline snapshot={runningSnapshot} operations={[verifiedOperation]} detailMode="developer" />)
    expect(details).toHaveAttribute('open')
    fireEvent.click(screen.getByText(/Operation details/))
    expect(details).not.toHaveAttribute('open')

    rerender(<ExecutionTimeline snapshot={runningSnapshot} operations={[verifiedOperation]} detailMode="simple" />)
    expect(details).not.toHaveAttribute('open')
  })

  it('filters operation and event diagnostics to the selected Run', () => {
    const unrelatedOperation: RunOperation = {
      ...verifiedOperation,
      run_id: 'another-run',
      operation_id: 'unrelated-operation',
    }
    const unrelatedEvent: RunEvent = {
      ...runEvent,
      run_id: 'another-run',
      seq: 15,
      type: 'unrelated.secret_event',
    }
    render(
      <ExecutionTimeline
        snapshot={runningSnapshot}
        operations={[verifiedOperation, unrelatedOperation]}
        events={[runEvent, unrelatedEvent]}
        detailMode="developer"
      />,
    )

    expect(screen.getByText('1 verified · 1 operation')).toBeInTheDocument()
    expect(screen.getByText(/#14 operation.verified/)).toBeInTheDocument()
    expect(screen.queryByText(/#15 unrelated.secret_event/)).not.toBeInTheDocument()
    fireEvent.click(screen.getByText(/Operation details/))
    expect(screen.getByText('op-file-write')).toBeInTheDocument()
    expect(screen.queryByText('unrelated-operation')).not.toBeInTheDocument()
  })
})
