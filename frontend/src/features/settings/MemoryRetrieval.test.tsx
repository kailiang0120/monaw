import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { MemoryRetrieval } from './MemoryRetrieval'
import { apiFetch } from '../../lib/api/client'
vi.mock('../../lib/api/client', () => ({ BASE: 'http://localhost', apiFetch: vi.fn() }))
const memory = { enabled: true, auto_learn: true, retrieval_limit: 6, max_injected_chars: 2500, min_relevance_score: 0.15, semantic_search: 'local' as const, embedding_model: 'model', embedding_endpoint: 'http://127.0.0.1:123/v1/embeddings' }
const keys = { openai: true, gemini: true }
const noKeys = { openai: false, gemini: false }
const ok = (data: unknown) => ({ ok: true, json: async () => data }) as Response
describe('MemoryRetrieval', () => {
  beforeEach(() => vi.resetAllMocks())
  afterEach(cleanup)
  it('edits settings and previews reasons and prompt', async () => {
    const change = vi.fn()
    vi.mocked(apiFetch).mockResolvedValue(ok({ results: [{ id: 'm-1', content: 'Likes tea', scope: 'global', match_type: 'semantic' }], prompt: '## Memory\n- Likes tea' }))
    render(<MemoryRetrieval memory={memory} onChange={change} keys={keys} chatProvider="openai" />)
    fireEvent.click(screen.getByRole('button', { name: 'Memory search' }))
    fireEvent.click(screen.getByRole('option', { name: 'Google' }))
    expect(change).toHaveBeenCalledWith(expect.objectContaining({ semantic_search: 'gemini' }))
    fireEvent.change(screen.getByLabelText('Preview memory for a message'), { target: { value: 'tea' } })
    fireEvent.click(screen.getByRole('button', { name: 'Preview' }))
    expect(await screen.findByText('Likes tea (global; semantic)')).toBeInTheDocument()
    expect(screen.getByText(/## Memory/)).toBeInTheDocument()
  })
  it('reports rebuild and errors', async () => {
    vi.mocked(apiFetch).mockResolvedValueOnce(ok({ status: 'ready', entries: 3 })).mockRejectedValueOnce(new Error('offline'))
    render(<MemoryRetrieval memory={memory} onChange={vi.fn()} keys={keys} chatProvider="openai" />)
    fireEvent.click(screen.getByRole('button', { name: 'Rebuild index' }))
    expect(await screen.findByRole('status')).toHaveTextContent('3 memories indexed')
    fireEvent.click(screen.getByRole('button', { name: 'Check status' }))
    expect(await screen.findByRole('status')).toHaveTextContent('offline')
  })
  it('ignores outdated preview responses', async () => {
    let resolve!: (r: Response) => void
    vi.mocked(apiFetch).mockReturnValue(new Promise(r => { resolve = r }))
    render(<MemoryRetrieval memory={memory} onChange={vi.fn()} keys={keys} chatProvider="openai" />)
    fireEvent.change(screen.getByLabelText('Preview memory for a message'), { target: { value: 'old' } })
    fireEvent.click(screen.getByRole('button', { name: 'Preview' }))
    fireEvent.change(screen.getByLabelText('Preview memory for a message'), { target: { value: 'new' } })
    resolve(ok({ results: [{ id: 'old', content: 'Old result', scope: 'global', match_type: 'keyword' }], prompt: 'Old result' }))
    await waitFor(() => expect(screen.queryByText('Old result')).not.toBeInTheDocument())
  })
  it('uses the saved key of the chat provider automatically without asking for a model', () => {
    render(<MemoryRetrieval memory={{ ...memory, semantic_search: 'auto' }} onChange={vi.fn()} keys={keys} chatProvider="gemini" />)

    expect(screen.getByText(/saved Google API key/)).toBeInTheDocument()
    expect(screen.queryByLabelText('Embedding model')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Rebuild index' })).toBeInTheDocument()
  })

  it('stays on keywords and asks for a key when none is saved', () => {
    render(<MemoryRetrieval memory={{ ...memory, semantic_search: 'auto' }} onChange={vi.fn()} keys={noKeys} chatProvider="codex" />)

    expect(screen.getByText(/Add an OpenAI or Google API key/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Rebuild index' })).not.toBeInTheDocument()
  })

  it('shows keyword fallback when semantic search is degraded', async () => {
    vi.mocked(apiFetch).mockResolvedValue(ok({ results: [], prompt: '', health: { status: 'degraded', error: 'Using keyword retrieval.' } }))
    render(<MemoryRetrieval memory={memory} onChange={vi.fn()} keys={keys} chatProvider="openai" />)
    fireEvent.change(screen.getByLabelText('Preview memory for a message'), { target: { value: 'tea' } })
    fireEvent.click(screen.getByRole('button', { name: 'Preview' }))
    expect(await screen.findByRole('status')).toHaveTextContent('Using keyword retrieval.')
  })

})
