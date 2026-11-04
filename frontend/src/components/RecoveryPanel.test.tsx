import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { ApiError } from '../lib/api/client'
import { fetchUndoCapability } from '../lib/api/fileMutations'
import type { RunOperation, RunSnapshot } from '../lib/api/runs'
import { RecoveryPanel } from './RecoveryPanel'

vi.mock('../lib/api/fileMutations', () => ({
  fetchUndoCapability: vi.fn(),
}))

const snapshot: RunSnapshot = {
  run_id: 'run-recovery-ui',
  conversation_id: 'chat-recovery-ui',
  attempt: 1,
  revision: 8,
  status: 'completed',
  reason_code: null,
  available_actions: ['inspect'],
  last_seq: 14,
  created_at: '2026-11-03T01:00:00Z',
  updated_at: '2026-11-03T01:10:00Z',
  finished_at: '2026-11-03T01:10:00Z',
}

const operation: RunOperation = {
  run_id: snapshot.run_id,
  operation_id: 'op-verified-write',
  sequence: 3,
  attempt: 1,
  operation_kind: 'file_write',
  related_operation_id: null,
  effect_class: 'verifiable_write',
  status: 'verified',
  reason_code: 'verified',
  input_summary: { path: 'report.txt' },
  evidence: { verification_result: 'completed' },
  snapshot_available: true,
  created_at: '2026-11-03T01:01:00Z',
  updated_at: '2026-11-03T01:02:00Z',
}

function capability(status: 'available' | 'unavailable') {
  return {
    run_id: snapshot.run_id,
    operation_id: operation.operation_id,
    status,
    reason_code: null,
    revision: snapshot.revision,
  } as const
}

function undoResult(status: string) {
  return {
    run_id: snapshot.run_id,
    operation_id: operation.operation_id,
    undo_operation_id: 'op-undo-file-write',
    status,
    reason_code: status === 'needs_review' ? 'external_effect_unknown' : null,
    evidence: {},
  }
}

describe('RecoveryPanel Undo controls', () => {
  afterEach(() => {
    cleanup()
    vi.resetAllMocks()
  })

  it('requires an available capability and a keyboard-accessible confirmation before Undo', async () => {
    vi.mocked(fetchUndoCapability).mockResolvedValue(capability('available'))
    const onUndo = vi.fn().mockResolvedValue(undoResult('undone'))
    render(<RecoveryPanel snapshot={snapshot} operations={[operation]} onUndo={onUndo} />)

    const undo = await screen.findByRole('button', { name: 'Undo' })
    fireEvent.click(undo)
    const confirmation = screen.getByRole('group', { name: 'Undo this file change?' })
    const cancel = screen.getByRole('button', { name: 'Cancel' })
    expect(document.activeElement).toBe(cancel)
    expect(screen.getByRole('button', { name: 'Undo now' })).toBeInTheDocument()

    fireEvent.keyDown(confirmation, { key: 'Escape' })
    expect(screen.queryByRole('group', { name: 'Undo this file change?' })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Undo' }))
    fireEvent.click(screen.getByRole('button', { name: 'Undo now' }))

    await waitFor(() => expect(onUndo).toHaveBeenCalledWith(operation, capability('available')))
    expect(await screen.findByText('This change has already been undone')).toBeInTheDocument()
    expect(fetchUndoCapability).toHaveBeenCalledWith(
      operation.operation_id,
      snapshot.run_id,
      snapshot.conversation_id,
      expect.any(AbortSignal),
    )
  })

  it('shows undo_conflict without claiming the file change was undone', async () => {
    vi.mocked(fetchUndoCapability).mockResolvedValue(capability('available'))
    const conflict = new ApiError(
      new Response(null, { status: 409 }),
      { code: 'undo_conflict', message: 'File changed after Apply.', request_id: '', details: {} },
      'Undo failed',
    )
    const onUndo = vi.fn().mockRejectedValue(conflict)
    const onRefresh = vi.fn()
    const { rerender } = render(<RecoveryPanel snapshot={snapshot} operations={[operation]} onUndo={onUndo} onRefresh={onRefresh} />)

    await screen.findByRole('button', { name: 'Undo' })
    rerender(<RecoveryPanel snapshot={snapshot} operations={[{ ...operation, snapshot_available: false }]} onUndo={onUndo} onRefresh={onRefresh} />)
    expect(screen.getByRole('button', { name: 'Undo' })).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Undo' }))
    fireEvent.click(screen.getByRole('button', { name: 'Undo now' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('File changed after this operation. Undo was not applied.')
    expect(screen.getByText('File write')).toBeInTheDocument()
    expect(onRefresh).toHaveBeenCalled()
  })

  it('keeps Undo unavailable when the route reports no safe inverse action', async () => {
    vi.mocked(fetchUndoCapability).mockResolvedValue(capability('unavailable'))
    render(<RecoveryPanel snapshot={snapshot} operations={[operation]} onUndo={vi.fn().mockResolvedValue(undoResult('undone'))} />)

    expect(await screen.findByText('Undo is unavailable for this operation')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Undo' })).not.toBeInTheDocument()
  })

  it.each(['needs_review', 'in_progress'])('does not report an HTTP 200 %s Undo result as completed', async (status) => {
    vi.mocked(fetchUndoCapability).mockResolvedValue(capability('available'))
    const onUndo = vi.fn().mockResolvedValue(undoResult(status))
    render(<RecoveryPanel snapshot={snapshot} operations={[operation]} onUndo={onUndo} />)

    await screen.findByRole('button', { name: 'Undo' })
    fireEvent.click(screen.getByRole('button', { name: 'Undo' }))
    fireEvent.click(screen.getByRole('button', { name: 'Undo now' }))

    expect(await screen.findByText(
      `Undo returned ${status === 'needs_review' ? 'Needs review' : 'In progress'}. Check the latest status before retrying.`,
    )).toBeInTheDocument()
    expect(screen.queryByText('This change has already been undone')).not.toBeInTheDocument()
    expect(screen.getByText('File write')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Refresh' })).toBeInTheDocument()
  })
})
