import { useState } from 'react'
import { FolderOpen, Loader2, X } from 'lucide-react'

interface Props {
  value: string
  disabled?: boolean
  onChange: (path: string) => Promise<void>
}

export function WorkspacePicker({ value, disabled, onChange }: Props) {
  const [busy, setBusy] = useState(false)
  const [editing, setEditing] = useState(false)
  const [path, setPath] = useState('')
  const [error, setError] = useState('')
  const blocked = disabled || busy
  const folderName = value.split(/[\\/]/).filter(Boolean).pop() || value

  const save = async (next: string) => {
    setBusy(true)
    setError('')
    try {
      await onChange(next)
      setEditing(false)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Could not change the working folder')
    } finally {
      setBusy(false)
    }
  }

  const choose = async () => {
    setError('')
    if (!window.electronAPI?.selectDirectory) {
      setPath(value)
      setEditing(true)
      return
    }
    setBusy(true)
    try {
      const selected = await window.electronAPI.selectDirectory(value || undefined)
      if (selected) await save(selected)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Could not open the folder picker')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="mb-2 text-xs text-neutral-400">
      <div className="flex min-w-0 items-center gap-2">
        <button type="button" onClick={() => void choose()} disabled={blocked}
          aria-label="Choose working folder" title={value || 'Use the default workspace, or choose a folder for this chat'}
          className="flex min-w-0 items-center gap-1.5 rounded-lg px-2 py-1 transition-colors hover:bg-white/[0.06] hover:text-neutral-200 disabled:opacity-50">
          {busy ? <Loader2 size={14} className="shrink-0 animate-spin" /> : <FolderOpen size={14} className="shrink-0" />}
          <span className="truncate">{value ? folderName : 'Default workspace'}</span>
        </button>
        {value && <button type="button" onClick={() => void save('')} disabled={blocked}
          aria-label="Use default workspace" title="Use default workspace"
          className="rounded p-1 hover:bg-white/[0.06] hover:text-neutral-200 disabled:opacity-50"><X size={13} /></button>}
      </div>
      {editing && (
        <form className="mt-2 flex gap-2" onSubmit={(event) => { event.preventDefault(); void save(path.trim()) }}>
          <input autoFocus value={path} onChange={(event) => setPath(event.target.value)} disabled={blocked}
            aria-label="Working folder path" placeholder="Absolute folder path"
            className="min-w-0 flex-1 rounded-lg border border-white/10 bg-white/[0.03] px-2 py-1 text-neutral-200" />
          <button type="submit" disabled={blocked || !path.trim()} className="rounded-lg px-2 py-1 hover:bg-white/[0.06] disabled:opacity-50">Use folder</button>
          <button type="button" disabled={busy} onClick={() => { setEditing(false); setError('') }} className="rounded-lg px-2 py-1 hover:bg-white/[0.06]">Cancel</button>
        </form>
      )}
      {error && <p role="alert" className="mt-1 text-red-400">{error}</p>}
    </div>
  )
}
