import { useEffect, useRef, useState } from 'react'
import { X } from 'lucide-react'
import { fetchConversations } from '../lib/api/conversations'
import type { Conversation } from '../lib/api/types'

const PAGE_SIZE = 50

interface Props {
  onClose: () => void
  onSelect: (conversation: Conversation) => void
}

/** Search and reopen any saved chat without expanding the main sidebar. */
export function ConversationHistoryDialog({ onClose, onSelect }: Props) {
  const dialogRef = useRef<HTMLDialogElement>(null)
  const searchRef = useRef<HTMLInputElement>(null)
  const requestRef = useRef<AbortController | null>(null)
  const generation = useRef(0)
  const [query, setQuery] = useState('')
  const [chats, setChats] = useState<Conversation[]>([])
  const [hasMore, setHasMore] = useState(false)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  /** Discard stale searches and append pages using the last activity cursor. */
  const load = async (search: string, after?: Conversation) => {
    requestRef.current?.abort()
    const controller = new AbortController()
    requestRef.current = controller
    const request = ++generation.current
    setLoading(true)
    setError('')
    try {
      const page = await fetchConversations(PAGE_SIZE + 1, 0, {
        search,
        beforeUpdatedAt: after?.updated_at ?? after?.created_at,
        beforeId: after?.id,
        signal: controller.signal,
      })
      if (controller.signal.aborted || request !== generation.current) return
      setChats((current) => {
        const merged = new Map((after ? current : []).map((chat) => [chat.id, chat]))
        for (const chat of page.slice(0, PAGE_SIZE)) merged.set(chat.id, chat)
        return [...merged.values()]
      })
      setHasMore(page.length > PAGE_SIZE)
    } catch (caught) {
      if (!controller.signal.aborted && request === generation.current) {
        setError(caught instanceof Error ? caught.message : 'Could not load chat history.')
      }
    } finally {
      if (!controller.signal.aborted && request === generation.current) setLoading(false)
    }
  }

  useEffect(() => {
    if (!dialogRef.current?.open) dialogRef.current?.showModal()
    searchRef.current?.focus({ preventScroll: true })
    return () => requestRef.current?.abort()
  }, [])

  useEffect(() => {
    requestRef.current?.abort()
    generation.current += 1
    setChats([])
    setHasMore(false)
    const timer = window.setTimeout(() => void load(query), query ? 200 : 0)
    return () => window.clearTimeout(timer)
  // Search owns the request generation; load reads no changing search state.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [query])

  return (
    <dialog
      ref={dialogRef}
      onClose={onClose}
      aria-labelledby="chat-history-title"
      className="settings-shell m-auto w-[min(36rem,90vw)] rounded-2xl p-4 backdrop:bg-black/60"
    >
      <div className="mb-3 flex items-center justify-between">
        <h2 id="chat-history-title" className="st-title">Chat history</h2>
        <button type="button" aria-label="Close chat history" className="st-btn st-btn-ghost st-btn-icon" onClick={() => dialogRef.current?.close()}>
          <X size={16} />
        </button>
      </div>
      <label htmlFor="chat-history-search" className="sr-only">Search all chats</label>
      <input
        ref={searchRef}
        id="chat-history-search"
        type="search"
        value={query}
        maxLength={200}
        onChange={(event) => setQuery(event.target.value)}
        placeholder="Search chats, messages, or folders"
        className="st-input mb-3 w-full"
      />
      <ul className="st-scroll max-h-[55vh] space-y-1 overflow-y-auto" aria-busy={loading}>
        {chats.map((chat) => (
          <li key={chat.id}>
            <button
              type="button"
              className="st-nav-item w-full rounded-lg px-3 py-2 text-left"
              onClick={() => { onSelect(chat); dialogRef.current?.close() }}
            >
              <span className="min-w-0 flex-1">
                <span className="block truncate text-sm">{chat.title}</span>
                <span className="st-hint block truncate">
                  {new Date(chat.updated_at ?? chat.created_at).toLocaleDateString()}
                  {chat.workspace_is_default
                    ? ' · Default workspace'
                    : chat.workspace_dir ? ` · ${chat.workspace_dir}` : ''}
                </span>
              </span>
            </button>
          </li>
        ))}
      </ul>
      {error && <p role="alert" className="py-2 text-sm text-red-300">{error}</p>}
      {!loading && !error && chats.length === 0 && <p className="st-desc py-3">No chats found.</p>}
      {(hasMore || error) && (
        <button type="button" className="st-btn st-btn-ghost mt-2 min-h-9 w-full" disabled={loading} onClick={() => void load(query, chats[chats.length - 1])}>
          {loading ? 'Loading…' : error ? 'Retry' : 'Load older chats'}
        </button>
      )}
    </dialog>
  )
}
