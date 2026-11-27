import { useId, useRef, useState } from 'react'
import { Download, FolderOpen, Loader2, X } from 'lucide-react'
import { inspectSkills, installSkills, type SkillImportPreview } from '../../lib/api/skills'
import type { AgentSettings } from '../../lib/api/types'
import { SettingsCard } from '../../components/ui/Panel'

/** Preview and install portable skills without submitting unrelated settings edits. */
export function SkillImport({ onInstalled }: { onInstalled: (settings: AgentSettings, names: string[]) => void }) {
  const [open, setOpen] = useState(false)
  const [source, setSource] = useState('')
  const [file, setFile] = useState<File | null>(null)
  const [preview, setPreview] = useState<SkillImportPreview | null>(null)
  const [selected, setSelected] = useState<string[]>([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const fieldId = useId()
  const errorRef = useRef<HTMLParagraphElement>(null)
  const previewRef = useRef<HTMLFieldSetElement>(null)
  const fileRef = useRef<HTMLInputElement>(null)

  /** Invalidate the old preview when its source changes. */
  const resetPreview = () => {
    setPreview(null)
    setSelected([])
    setError('')
    setMessage('')
  }

  /** Announce a failed operation and focus its explanation. */
  const showError = (cause: unknown) => {
    setError(cause instanceof Error ? cause.message : 'Could not import skills.')
    requestAnimationFrame(() => errorRef.current?.focus())
  }

  /** Select an existing local skill folder through the desktop bridge. */
  const browse = async () => {
    setBusy(true)
    try {
      const path = await window.electronAPI?.selectDirectory?.(source || undefined)
      if (path) {
        resetPreview()
        setSource(path)
        setFile(null)
        if (fileRef.current) fileRef.current.value = ''
      }
    } catch (cause) {
      showError(cause)
    } finally {
      setBusy(false)
    }
  }

  /** List source skills before any files are installed. */
  const inspect = async () => {
    setBusy(true)
    setError('')
    setMessage('')
    try {
      let payload: { source?: string; filename?: string; data_base64?: string } = { source: source.trim() }
      if (file) {
        if (file.size > 6 * 1024 * 1024) throw new Error('Choose a ZIP or SKILL.md file up to 6 MB. Use a local folder for larger skills.')
        const bytes = new Uint8Array(await file.arrayBuffer())
        let binary = ''
        for (let offset = 0; offset < bytes.length; offset += 8192) {
          binary += String.fromCharCode(...bytes.subarray(offset, offset + 8192))
        }
        payload = { filename: file.name, data_base64: btoa(binary) }
      }
      if (!file && !source.trim()) throw new Error('Enter a GitHub source, choose a folder, or select a file.')
      const result = await inspectSkills(payload)
      setPreview(result)
      setSelected(result.skills.filter((skill) => !skill.installed).map((skill) => skill.id))
      requestAnimationFrame(() => previewRef.current?.focus())
    } catch (cause) {
      showError(cause)
    } finally {
      setBusy(false)
    }
  }

  /** Activate only the selected skills immediately. */
  const install = async () => {
    if (!preview) return
    setBusy(true)
    setError('')
    try {
      const result = await installSkills(preview.token, selected)
      onInstalled(result.settings, result.installed)
      setPreview(null)
      setSelected([])
      setSource('')
      setFile(null)
      if (fileRef.current) fileRef.current.value = ''
      setMessage(`Installed ${result.installed.join(', ')}. Ready for your next message.`)
    } catch (cause) {
      showError(cause)
    } finally {
      setBusy(false)
    }
  }

  return (
    <SettingsCard title="Import skills" action={
      <button type="button" className={`st-btn ${open ? 'st-btn-ghost' : 'st-btn-secondary'}`} aria-expanded={open} aria-controls={`${fieldId}-panel`}
        disabled={busy} onClick={() => setOpen((value) => !value)}>
        {open ? <X size={14} aria-hidden="true" /> : <Download size={14} aria-hidden="true" />}
        {open ? 'Close' : 'Import'}
      </button>
    }>
      {open && (
        <div id={`${fieldId}-panel`} className="st-skill-import" aria-busy={busy}>
          <form aria-label="Skill source" method="post" onSubmit={(event) => { event.preventDefault(); void inspect() }}>
            <div className="st-row-stacked">
              <label htmlFor={`${fieldId}-source`} className="st-label mb-2 block">GitHub URL or local folder</label>
              <div className="flex items-center gap-2">
                <input id={`${fieldId}-source`} name="source" className="st-input min-w-0 flex-1"
                  value={source} disabled={busy} placeholder="owner/repo or GitHub skill folder URL"
                  autoCapitalize="none" spellCheck={false} onChange={(event) => {
                    resetPreview()
                    setSource(event.target.value)
                    setFile(null)
                    if (fileRef.current) fileRef.current.value = ''
                  }} />
                {window.electronAPI?.selectDirectory && (
                  <button type="button" className="st-btn st-btn-secondary shrink-0" disabled={busy} onClick={() => void browse()}>
                    <FolderOpen size={14} aria-hidden="true" />Browse
                  </button>
                )}
              </div>
            </div>
            <div className="st-row-stacked">
              <label htmlFor={`${fieldId}-file`} className="st-label mb-2 block">Or import a ZIP or SKILL.md file</label>
              <input id={`${fieldId}-file`} ref={fileRef} type="file" name="skill_file" accept=".zip,.md"
                disabled={busy} className="st-skill-file" onChange={(event) => {
                  resetPreview()
                  setFile(event.target.files?.[0] ?? null)
                  setSource('')
                }} />
            </div>
            <div className="st-skill-import-actions">
              <button type="submit" className="st-btn st-btn-primary" disabled={busy}>
                {busy && <Loader2 size={14} className="animate-spin" aria-hidden="true" />}
                Check source
              </button>
            </div>
          </form>
          {preview && (
            <fieldset ref={previewRef} tabIndex={-1} className="m-3 space-y-2 rounded-lg border border-[var(--st-border)] p-3">
              <legend className="st-label px-1">Choose skills</legend>
              <div className="max-h-64 space-y-3 overflow-y-auto">
                {preview.skills.map((skill) => (
                  <label key={skill.id} className="flex items-start gap-2">
                    <input type="checkbox" className="mt-1" disabled={busy || skill.installed} checked={selected.includes(skill.id)}
                      onChange={(event) => setSelected((current) => event.target.checked ? [...current, skill.id] : current.filter((id) => id !== skill.id))} />
                    <span className="min-w-0">
                      <span className="st-label block">{skill.name}{skill.installed ? ' · Already installed' : ''}</span>
                      <span className="st-desc block break-words">{skill.description}</span>
                    </span>
                  </label>
                ))}
              </div>
              <p className="st-hint">Installs skills and their files. Plugin hooks, MCP connections, and dependencies need separate setup.</p>
              <button type="button" disabled={busy || !selected.length} className="st-btn st-btn-primary" onClick={() => void install()}>
                Install {selected.length || ''}{selected.length === 1 ? ' skill' : ' skills'}
              </button>
            </fieldset>
          )}
          {error && <p ref={errorRef} tabIndex={-1} role="alert" className="m-3 text-sm text-[var(--st-danger)]">{error}</p>}
          {message && <p role="status" className="st-desc m-3">{message}</p>}
        </div>
      )}
    </SettingsCard>
  )
}
