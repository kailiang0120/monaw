import { useEffect, useState } from 'react'
import { Search } from 'lucide-react'
import { searchMemories } from '../lib/api/memories'
import type { MemorySearchRecord } from '../lib/api/types'

interface Props {
  initialQuery?: string
}

export function MemoryRetrievalDebugger({ initialQuery = '' }: Props) {
  const [query, setQuery] = useState(initialQuery)
  const [results, setResults] = useState<MemorySearchRecord[]>([])
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    setQuery(initialQuery)
  }, [initialQuery])

  const runSearch = async (value = query) => {
    const trimmed = value.trim()
    if (!trimmed) {
      setResults([])
      return
    }
    setLoading(true)
    try {
      setResults(await searchMemories({ query: trimmed, limit: 5 }))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    const trimmed = initialQuery.trim()
    if (!trimmed) return
    runSearch(trimmed).catch(() => setResults([]))
  }, [initialQuery])

  return (
    <div className="rounded-xl border border-white/[0.07] bg-black/10 p-3">
      <div className="flex flex-col gap-2 sm:flex-row">
        <div className="relative min-w-0 flex-1">
          <Search size={14} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-neutral-600" />
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter') runSearch().catch(() => setResults([]))
            }}
            placeholder="Debug retrieval query"
            className="control w-full rounded-xl py-2 pl-9 pr-3 text-sm"
          />
        </div>
        <button
          type="button"
          onClick={() => runSearch().catch(() => setResults([]))}
          disabled={!query.trim() || loading}
          className="ghost-button rounded-xl px-3 py-2 text-sm disabled:cursor-not-allowed disabled:opacity-50"
        >
          Search
        </button>
      </div>

      {results.length > 0 && (
        <div className="mt-3 space-y-2">
          {results.map((memory) => (
            <div key={memory.id} className="rounded-lg border border-white/[0.06] bg-white/[0.025] px-3 py-2">
              <div className="mb-1 flex flex-wrap items-center gap-2 text-[11px] text-neutral-500">
                <span>#{memory.id}</span>
                <span>{formatScore(memory.score)}</span>
                <span>exact {formatScore(memory.score_breakdown.exact)}</span>
                <span>token {formatScore(memory.score_breakdown.token)}</span>
                <span>cat {formatScore(memory.score_breakdown.category)}</span>
                <span>rec {formatScore(memory.score_breakdown.recency)}</span>
                <span>imp {formatScore(memory.score_breakdown.importance)}</span>
                <span>use {formatScore(memory.score_breakdown.use)}</span>
              </div>
              <p className="break-words text-xs leading-relaxed text-neutral-300">{memory.content}</p>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

function formatScore(value: number) {
  return Number.isFinite(value) ? value.toFixed(2) : '0.00'
}
