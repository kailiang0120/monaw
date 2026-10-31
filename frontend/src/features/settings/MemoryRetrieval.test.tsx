import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { MemoryRetrieval } from './MemoryRetrieval'
import { apiFetch } from '../../lib/api/client'
vi.mock('../../lib/api/client', () => ({ BASE: 'http://localhost', apiFetch: vi.fn() }))
const memory = { enabled: true, auto_learn: true, retrieval_limit: 6, max_injected_chars: 2500, min_relevance_score: 0.15, embedding_mode: 'local' as const, embedding_model: 'model', embedding_endpoint: 'http://127.0.0.1:123/v1/embeddings' }
const ok = (data: unknown) => ({ ok: true, json: async () => data }) as Response
describe('MemoryRetrieval', () => {
  beforeEach(() => vi.resetAllMocks())
  afterEach(cleanup)
  it('edits settings and previews reasons and prompt', async () => {
    const change = vi.fn()
    vi.mocked(apiFetch).mockResolvedValue(ok({ results: [{ id: 'm-1', content: 'Likes tea', scope: 'global', match_type: 'semantic' }], prompt: '## Memory\n- Likes tea' }))
    render(<MemoryRetrieval memory={memory} onChange={change} />)
    fireEvent.change(screen.getByLabelText('Memory retrieval'), { target: { value: 'provider' } })
    expect(change).toHaveBeenCalledWith(expect.objectContaining({ embedding_mode: 'provider' }))
    fireEvent.change(screen.getByLabelText('Preview memory for a message'), { target: { value: 'tea' } })
    fireEvent.click(screen.getByRole('button', { name: 'Preview' }))
    expect(await screen.findByText('Likes tea (global; semantic)')).toBeInTheDocument()
    expect(screen.getByText(/## Memory/)).toBeInTheDocument()
  })
  it('reports rebuild and errors', async () => {
    vi.mocked(apiFetch).mockResolvedValueOnce(ok({ status: 'ready', entries: 3 })).mockRejectedValueOnce(new Error('offline'))
    render(<MemoryRetrieval memory={memory} onChange={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: 'Rebuild index' }))
    expect(await screen.findByRole('status')).toHaveTextContent('3 memories indexed')
    fireEvent.click(screen.getByRole('button', { name: 'Check status' }))
    expect(await screen.findByRole('status')).toHaveTextContent('offline')
  })
  it('ignores outdated preview responses', async () => {
    let resolve!: (r: Response) => void
    vi.mocked(apiFetch).mockReturnValue(new Promise(r => { resolve = r }))
    render(<MemoryRetrieval memory={memory} onChange={vi.fn()} />)
    fireEvent.change(screen.getByLabelText('Preview memory for a message'), { target: { value: 'old' } })
    fireEvent.click(screen.getByRole('button', { name: 'Preview' }))
    fireEvent.change(screen.getByLabelText('Preview memory for a message'), { target: { value: 'new' } })
    resolve(ok({ results: [{ id: 'old', content: 'Old result', scope: 'global', match_type: 'keyword' }], prompt: 'Old result' }))
    await waitFor(() => expect(screen.queryByText('Old result')).not.toBeInTheDocument())
  })
  it('shows keyword fallback when semantic search is degraded', async () => {
    vi.mocked(apiFetch).mockResolvedValue(ok({ results: [], prompt: '', health: { status: 'degraded', error: 'Using keyword retrieval.' } }))
    render(<MemoryRetrieval memory={memory} onChange={vi.fn()} />)
    fireEvent.change(screen.getByLabelText('Preview memory for a message'), { target: { value: 'tea' } })
    fireEvent.click(screen.getByRole('button', { name: 'Preview' }))
    expect(await screen.findByRole('status')).toHaveTextContent('Using keyword retrieval.')
  })

})
