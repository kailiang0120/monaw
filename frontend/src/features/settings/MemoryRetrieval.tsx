import { useEffect, useRef, useState } from 'react'
import { apiFetch, BASE } from '../../lib/api/client'
import type { AgentSettings } from '../../lib/api/types'
import { Dropdown } from '../../components/Dropdown'
import { SettingRow } from '../../components/ui/Panel'
import { activeKeyProvider } from './settingsConfig'

type Memory = AgentSettings['memory']
type SemanticSearch = NonNullable<Memory['semantic_search']>
type Result = { id: string; content: string; scope: string; relevance: number; match_type: string }

const PROVIDER_NAMES = { openai: 'OpenAI', gemini: 'Google' } as const

/** The embedding provider memory search uses, or '' for keywords only. Mirrors the backend. */
export function activeMemoryProvider(choice: SemanticSearch, keys: { openai: boolean; gemini: boolean }, chatProvider: string): '' | 'openai' | 'gemini' | 'local' {
  if (choice === 'off') return ''
  if (choice === 'local') return 'local'
  return activeKeyProvider(choice, keys, chatProvider)
}

function searchSummary(choice: SemanticSearch, provider: ReturnType<typeof activeMemoryProvider>): string {
  if (provider === 'openai' || provider === 'gemini') {
    const name = PROVIDER_NAMES[provider]
    return `Matches memories by meaning with your saved ${name} API key. Memory text is sent to ${name}; usage is billed to that key.`
  }
  if (provider === 'local') return 'Matches memories by meaning with an embeddings service running on this PC.'
  if (choice === 'off') return 'Matches memories by shared words only.'
  if (choice === 'auto') return 'Matching by shared words. Add an OpenAI or Google API key in Connections to also match by meaning.'
  return `Add ${choice === 'openai' ? 'an OpenAI' : 'a Google'} API key in Connections to use it.`
}

export function MemoryRetrieval({ memory, onChange, keys, chatProvider }: {
  memory: Memory
  onChange: (value: Memory) => void
  keys: { openai: boolean; gemini: boolean }
  chatProvider: string
}) {
  const [query, setQuery] = useState('')
  const [workspace, setWorkspace] = useState('')
  const [results, setResults] = useState<Result[] | null>(null)
  const [prompt, setPrompt] = useState('')
  const [status, setStatus] = useState('')
  const [busy, setBusy] = useState(false)
  const pending = useRef<AbortController | null>(null)
  useEffect(() => () => pending.current?.abort(), [])
  const choice: SemanticSearch = memory.semantic_search ?? 'auto'
  const provider = activeMemoryProvider(choice, keys, chatProvider)
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
      else setStatus(data.status === 'ready' ? `Ready: ${data.entries} memories indexed${data.model ? ` with ${data.model}` : ''}.` : data.status === 'disabled' ? (data.error || 'Keyword retrieval is active.') : `Unavailable: ${data.error || 'check the memory settings.'}`)
    } catch (error) { if (!controller.signal.aborted) setStatus(error instanceof Error ? error.message : 'Memory service unavailable') }
    finally { if (!controller.signal.aborted) { setBusy(false); pending.current = null } }
  }
  return <>
    <SettingRow label="Memory search" description={searchSummary(choice, provider)}>
      <Dropdown<SemanticSearch>
        ariaLabel="Memory search"
        value={choice}
        options={[
          { value: 'auto', label: 'Automatic' },
          { value: 'openai', label: 'OpenAI' },
          { value: 'gemini', label: 'Google' },
          { value: 'local', label: 'Local service' },
          { value: 'off', label: 'Keywords only' },
        ]}
        onChange={(semantic_search) => { invalidate(); onChange({ ...memory, semantic_search }) }}
      />
    </SettingRow>
    {choice === 'local' && <>
      <SettingRow label="Embedding model" description="The model name your local service serves. Download it there first." wide>
        <input className="st-input" aria-label="Embedding model" value={memory.embedding_model || ''} onChange={e => onChange({ ...memory, embedding_model: e.target.value })} />
      </SettingRow>
      <SettingRow label="Service address" description="Its OpenAI-compatible embeddings address on this PC." wide>
        <input className="st-input" aria-label="Local service address" placeholder="http://127.0.0.1:1234/v1/embeddings" value={memory.embedding_endpoint || ''} onChange={e => onChange({ ...memory, embedding_endpoint: e.target.value })} />
      </SettingRow>
    </>}
    <div className="space-y-3 px-4 py-3">
      {provider && <div className="flex flex-wrap items-center gap-2">
        <button type="button" className="st-btn st-btn-secondary" disabled={busy} onClick={() => void run('health')}>Check status</button>
        <button type="button" className="st-btn st-btn-secondary" disabled={busy} onClick={() => void run('rebuild')}>Rebuild index</button>
        <span className="st-desc">Save settings first.</span>
      </div>}
      <label className="block st-desc">Workspace folder (leave blank for default)<input className="st-input mt-1" value={workspace} onChange={e => { invalidate(); setWorkspace(e.target.value) }} /></label>
      <label className="block st-desc">Preview memory for a message<input className="st-input mt-1" value={query} onChange={e => { invalidate(); setQuery(e.target.value) }} /></label>
      <button type="button" className="st-btn st-btn-secondary" disabled={busy || !query.trim()} onClick={() => void run('preview')}>Preview</button>
      {status && <p role="status" className="st-desc">{status}</p>}
      {prompt && <><p className="st-label">Memory included in the prompt:</p><pre className="st-desc whitespace-pre-wrap">{prompt}</pre></>}
      {results && <ul className="st-desc">{results.length ? results.map(item => <li key={`${item.scope}:${item.id}`}>{item.content} ({item.scope}; {item.match_type})</li>) : <li>No matching memories.</li>}</ul>}
    </div>
  </>
}
