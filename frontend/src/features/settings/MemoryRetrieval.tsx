import { useEffect, useRef, useState } from 'react'
import { apiFetch, BASE } from '../../lib/api/client'
import type { AgentSettings } from '../../lib/api/types'

type Memory = AgentSettings['memory']
type Result = { id: string; content: string; scope: string; relevance: number; match_type: string }

export function MemoryRetrieval({ memory, onChange }: { memory: Memory; onChange: (value: Memory) => void }) {
  const [query, setQuery] = useState('')
  const [workspace, setWorkspace] = useState('')
  const [results, setResults] = useState<Result[] | null>(null)
  const [prompt, setPrompt] = useState('')
  const [status, setStatus] = useState('')
  const [busy, setBusy] = useState(false)
  const pending = useRef<AbortController | null>(null)
  useEffect(() => () => pending.current?.abort(), [])
  const invalidate = () => { pending.current?.abort(); pending.current = null; setBusy(false); setResults(null); setPrompt(''); setStatus('') }
  const run = async (action: 'preview' | 'health' | 'rebuild') => {
    pending.current?.abort()
    const controller = new AbortController()
    pending.current = controller
    setBusy(true)
    setResults(null)
    setPrompt('')
    setStatus('')
    try {
      const params = new URLSearchParams({ workspace })
      if (action === 'preview') params.set('query', query)
      const path = action === 'preview' ? 'inspect' : action === 'health' ? 'index/health' : 'index/rebuild'
      const response = await apiFetch(`${BASE}/api/memories/${path}?${params}`, { method: action === 'rebuild' ? 'POST' : 'GET', signal: controller.signal })
      if (!response.ok) throw new Error('Could not reach memory service')
      const data = await response.json()
      if (controller.signal.aborted) return
      if (action === 'preview') { setResults(data.results as Result[]); setPrompt(data.prompt || ''); if (data.health?.status === 'degraded' || data.health?.status === 'unconfigured') setStatus(data.health.error || 'Keyword retrieval is being used.') }
      else setStatus(data.status === 'ready' ? `Ready: ${data.entries} memories indexed.` : data.status === 'disabled' ? 'Keyword retrieval is active.' : `Unavailable: ${data.error || 'check the memory settings.'}`)
    } catch (error) { if (!controller.signal.aborted) setStatus(error instanceof Error ? error.message : 'Memory service unavailable') }
    finally { if (!controller.signal.aborted) { setBusy(false); pending.current = null } }
  }
  return <div className="space-y-3 py-3">
    <label className="block">Memory retrieval
      <select className="st-input" aria-label="Memory retrieval" value={memory.embedding_mode || 'keyword'} onChange={e => onChange({ ...memory, embedding_mode: e.target.value as Memory['embedding_mode'] })}>
        <option value="keyword">Keywords only</option><option value="local">Local model service</option><option value="provider">OpenAI embeddings</option>
      </select>
    </label>
    {memory.embedding_mode && memory.embedding_mode !== 'keyword' && <>
      <label className="block">Embedding model<input className="st-input" value={memory.embedding_model || ''} onChange={e => onChange({ ...memory, embedding_model: e.target.value })} /></label>
      {memory.embedding_mode === 'local' ? <><label className="block">Local service address<input className="st-input" placeholder="http://127.0.0.1:port/v1/embeddings" value={memory.embedding_endpoint || ''} onChange={e => onChange({ ...memory, embedding_endpoint: e.target.value })} /></label><p>Install a local embeddings service and download a model separately before enabling it.</p></> : <p>Uses your configured OpenAI API key. A ChatGPT sign-in does not provide API access. Memory text is sent to OpenAI and usage may be billed.</p>}
      <p>Save settings before checking or rebuilding the index.</p>
      <button type="button" className="st-btn st-btn-ghost" disabled={busy} onClick={() => void run('health')}>Check status</button>{' '}
      <button type="button" className="st-btn st-btn-ghost" disabled={busy} onClick={() => void run('rebuild')}>Rebuild index</button>
    </>}
    <label className="block">Workspace folder (leave blank for default)<input className="st-input" value={workspace} onChange={e => { invalidate(); setWorkspace(e.target.value) }} /></label>
    <label className="block">Preview memory for a message<input className="st-input" value={query} onChange={e => { invalidate(); setQuery(e.target.value) }} /></label>
    <button type="button" className="st-btn st-btn-ghost" disabled={busy || !query.trim()} onClick={() => void run('preview')}>Preview</button>
    {status && <p role="status">{status}</p>}
    {prompt && <><p>Memory included in the prompt:</p><pre className="whitespace-pre-wrap">{prompt}</pre></>}
    {results && <ul>{results.length ? results.map(item => <li key={`${item.scope}:${item.id}`}>{item.content} ({item.scope}; {item.match_type})</li>) : <li>No matching memories.</li>}</ul>}
  </div>
}
