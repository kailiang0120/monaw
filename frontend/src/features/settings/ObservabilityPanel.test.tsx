import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ObservabilityPanel } from './ObservabilityPanel'
import {
  deleteRuntimeData,
  fetchBackendLogTail,
  fetchObservabilityErrors,
  fetchObservabilityRun,
  fetchObservabilityRuns,
  fetchObservabilitySummary,
} from '../../lib/api/diagnostics'

const mocks = vi.hoisted(() => ({
  fetchObservabilitySummary: vi.fn(),
  fetchObservabilityRuns: vi.fn(),
  fetchObservabilityRun: vi.fn(),
  fetchObservabilityErrors: vi.fn(),
  fetchBackendLogTail: vi.fn(),
  fetchSupportMode: vi.fn(),
  enableSupportMode: vi.fn(),
  disableSupportMode: vi.fn(),
  replayObservabilityRun: vi.fn(),
  exportObservabilityDebugBundle: vi.fn(),
  deleteRuntimeData: vi.fn(),
}))

vi.mock('../../lib/api/diagnostics', () => mocks)

const summary = {
  total_runs: 2,
  successful_runs: 1,
  success_rate: 0.5,
  failed_runs: 1,
  total_tokens: 1234,
  estimated_cost_usd: 0.0123,
  average_duration_ms: 1500,
  tool_error_count: 1,
  storage_path: 'C:/Users/KL/.monaw/runtime/observability',
  token_usage_over_time: [{ day: '2026-05-23', runs: 2, tokens: 1234, average_duration_ms: 1500 }],
  duration_trend: [{ day: '2026-05-23', runs: 2, tokens: 1234, average_duration_ms: 1500 }],
  top_error_reasons: [{ reason: 'repeat_guard', count: 1 }],
  top_failing_tools: [{ tool_name: 'browser_click', count: 1 }],
  model_usage: [{ provider: 'openai', model: 'gpt-test', runs: 2, tokens: 1234 }],
  storage_metrics: { total_bytes: 2048 },
  runtime_metrics: { dropped_events: 0 },
  field_classification: {},
}

const run = {
  run_id: 'run_123456789',
  conversation_id: 'conv-1',
  message_id: '',
  source: 'desktop',
  model: 'gpt-test',
  provider: 'openai',
  status: 'paused',
  failure_reason: 'repeated_tool_call_blocked',
  failure_pattern: 'repeat_guard',
  started_at: '2026-05-23T00:00:00Z',
  finished_at: '2026-05-23T00:00:01Z',
  duration_ms: 1500,
  user_message: 'Check AMD',
  final_output: 'Stopped repeating.',
  input_tokens: 100,
  output_tokens: 20,
  reasoning_tokens: 0,
  cached_tokens: 0,
  image_tokens: 0,
  total_tokens: 120,
  usage_source: 'provider',
  estimated_cost_usd: 0,
  cost_source: 'unknown',
  tool_count: 2,
  tool_error_count: 1,
  event_count: 4,
}

describe('ObservabilityPanel', () => {
  beforeEach(() => {
    vi.mocked(fetchObservabilitySummary).mockResolvedValue(summary as any)
    vi.mocked(fetchObservabilityRuns).mockResolvedValue([run] as any)
    vi.mocked(fetchObservabilityRun).mockResolvedValue({
      ...run,
      events: [
        {
          event_id: 'evt-1',
          run_id: run.run_id,
          conversation_id: run.conversation_id,
          message_id: '',
          event_type: 'tool_call_finished',
          level: 'info',
          status: 'error',
          source: 'desktop',
          model: 'gpt-test',
          provider: 'openai',
          tool_name: 'browser_click',
          error_code: 'repeated_tool_call_blocked',
          error_message: 'blocked',
          duration_ms: 40,
          input: { arguments: { ref: '1' } },
          output: { output: 'blocked' },
          tokens: null,
          metadata: {},
          created_at: '2026-05-23T00:00:01Z',
        },
      ],
      errors: [],
      replays: [],
      tool_sequence: ['browser_click'],
    } as any)
    vi.mocked(fetchObservabilityErrors).mockResolvedValue([])
    vi.mocked(fetchBackendLogTail).mockResolvedValue({
      path: 'C:/Users/KL/.monaw/runtime/backend.log',
      exists: true,
      size_bytes: 10,
      lines: ['backend ready'],
      truncated: false,
    })
    vi.mocked(deleteRuntimeData).mockResolvedValue({
      ok: true,
      deleted: { observability_runs: 1, memory_files_deleted: 2 },
      deleted_at: '2026-05-23T00:00:02Z',
    })
  })

  afterEach(() => {
    cleanup()
    vi.clearAllMocks()
  })

  it('renders summary metrics and opens run detail', async () => {
    render(<ObservabilityPanel />)

    expect(await screen.findByText('Total runs')).toBeInTheDocument()
    expect(screen.getByText('1,234')).toBeInTheDocument()
    fireEvent.click(await screen.findByText('Check AMD'))

    await waitFor(() => {
      expect(fetchObservabilityRun).toHaveBeenCalledWith(run.run_id, expect.any(AbortSignal))
    })
    expect(await screen.findByText('Timeline')).toBeInTheDocument()
    expect(screen.getByText('tool_call_finished')).toBeInTheDocument()
    expect(screen.getAllByText('repeated_tool_call_blocked').length).toBeGreaterThan(0)
  })

  it('reloads view-specific data when switching panels', async () => {
    render(<ObservabilityPanel />)

    expect(await screen.findByText('Total runs')).toBeInTheDocument()

    vi.clearAllMocks()
    fireEvent.click(screen.getByRole('button', { name: 'Errors' }))

    await waitFor(() => {
      expect(fetchObservabilityErrors).toHaveBeenCalledWith({
        q: '',
        limit: 100,
        signal: expect.any(AbortSignal),
      })
    })

    vi.clearAllMocks()
    fireEvent.click(screen.getByRole('button', { name: 'Backend log' }))

    await waitFor(() => {
      expect(fetchBackendLogTail).toHaveBeenCalledWith(500, expect.any(AbortSignal))
    })
  })

  it('confirms and deletes runtime data', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)

    render(<ObservabilityPanel />)

    fireEvent.click(await screen.findByRole('button', { name: 'Delete Data' }))

    await waitFor(() => {
      expect(deleteRuntimeData).toHaveBeenCalled()
    })
    expect(confirmSpy).toHaveBeenCalled()
    expect(await screen.findByText('Runtime data deleted across 3 stored items.')).toBeInTheDocument()

    confirmSpy.mockRestore()
  })
})
