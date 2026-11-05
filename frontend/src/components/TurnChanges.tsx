import { useState } from 'react'
import { ChevronDown, FilePen, FilePlus2, FileX2, FolderPlus, Terminal } from 'lucide-react'

import type { ToolCall } from '../hooks/useChat'
import { collectTurnChanges, type TurnChange, type TurnChangeAction } from '../lib/turnChanges'

const COLLAPSED_LIMIT = 6

const ACTION_LABEL: Record<TurnChangeAction, string> = {
  created: 'Created',
  edited: 'Edited',
  appended: 'Appended',
  folder: 'New folder',
  copied: 'Copied',
  moved: 'Moved',
  deleted: 'Deleted',
}

function ActionIcon({ action }: { action: TurnChangeAction }) {
  if (action === 'deleted') return <FileX2 size={12} className="shrink-0 text-red-300" aria-hidden />
  if (action === 'folder') return <FolderPlus size={12} className="shrink-0 text-sky-300" aria-hidden />
  if (action === 'created' || action === 'copied') return <FilePlus2 size={12} className="shrink-0 text-emerald-300" aria-hidden />
  return <FilePen size={12} className="shrink-0 text-amber-200" aria-hidden />
}

function splitPath(path: string): { name: string; dir: string } {
  const normalized = path.replace(/[\\/]+$/, '')
  const index = Math.max(normalized.lastIndexOf('/'), normalized.lastIndexOf('\\'))
  return index < 0
    ? { name: normalized, dir: '' }
    : { name: normalized.slice(index + 1), dir: normalized.slice(0, index) }
}

function ChangeRow({ change }: { change: TurnChange }) {
  const { name, dir } = splitPath(change.path)
  return (
    <li className="flex min-w-0 items-center gap-2 px-3 py-1.5" title={change.from ? `${change.from} → ${change.path}` : change.path}>
      <ActionIcon action={change.action} />
      <span className={`shrink-0 font-mono text-xs ${change.action === 'deleted' ? 'text-neutral-400 line-through' : 'text-neutral-100'}`}>{name}</span>
      {dir && <span className="min-w-0 truncate font-mono text-[11px] text-neutral-500">{dir}</span>}
      <span className="ml-auto shrink-0 text-[10px] uppercase tracking-wide text-neutral-500">{ACTION_LABEL[change.action]}</span>
    </li>
  )
}

/** The files one reply wrote, at the end of that reply. */
export function TurnChanges({ toolCalls }: { toolCalls?: ToolCall[] }) {
  const [expanded, setExpanded] = useState(false)
  const { files, commands } = collectTurnChanges(toolCalls)
  if (files.length === 0 && commands === 0) return null
  const commandNote = `${commands === 1 ? 'Ran 1 command' : `Ran ${commands} commands`}; files changed by commands are not listed.`
  if (files.length === 0) {
    return (
      <p className="flex items-center gap-1.5 text-[11px] text-neutral-500" aria-label="Changes in this reply">
        <Terminal size={11} aria-hidden /> {commandNote}
      </p>
    )
  }
  const visible = expanded ? files : files.slice(0, COLLAPSED_LIMIT)
  const hidden = files.length - visible.length
  const title = `${files.length} ${files.length === 1 ? 'file' : 'files'} changed`

  return (
    <section className="overflow-hidden rounded-xl border border-white/[0.08] bg-white/[0.02]" aria-label="Changes in this reply">
      <header className="flex items-center gap-2 border-b border-white/[0.06] px-3 py-1.5 text-xs">
        <span className="font-medium text-neutral-200">Changes</span>
        <span className="text-neutral-500">{title}</span>
      </header>
      <ul className="divide-y divide-white/[0.04]">
          {visible.map((change) => <ChangeRow key={change.path} change={change} />)}
        </ul>
      {hidden > 0 && (
        <button
          type="button"
          onClick={() => setExpanded(true)}
          className="flex w-full items-center gap-1 border-t border-white/[0.06] px-3 py-1.5 text-[11px] text-neutral-400 hover:text-neutral-200 focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40"
        >
          <ChevronDown size={12} aria-hidden /> Show {hidden} more
        </button>
      )}
      {commands > 0 && (
        <p className="flex items-center gap-1.5 border-t border-white/[0.06] px-3 py-1.5 text-[11px] text-neutral-500">
          <Terminal size={11} aria-hidden /> {commandNote}
        </p>
      )}
    </section>
  )
}
