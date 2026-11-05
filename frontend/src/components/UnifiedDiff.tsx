interface UnifiedDiffProps {
  text: string
  label: string
  className?: string
  compact?: boolean
}

/** Keep the original diff text selectable, with theme-aware line highlighting. */
export function UnifiedDiff({ text, label, className = '', compact = true }: UnifiedDiffProps) {
  let before = 0
  let after = 0
  const lines = text.split('\n')
  return (
    <pre aria-label={label} className={`overflow-auto whitespace-pre-wrap break-words rounded-md border border-white/[0.05] bg-black/25 py-2 font-mono ${compact ? 'text-[10px]' : 'text-xs'} leading-relaxed text-neutral-300 ${className}`}>
      {lines.map((line, index) => {
        const hunk = /^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@/.exec(line)
        let kind = 'context'
        if (hunk) {
          before = Number(hunk[1] ?? 1)
          after = Number(hunk[2] ?? 1)
        } else {
          const inHunk = before > 0 || after > 0
          if (line.startsWith('+') && (inHunk || !line.startsWith('+++ '))) {
            kind = 'added'
            after = Math.max(0, after - 1)
          } else if (line.startsWith('-') && (inHunk || !line.startsWith('--- '))) {
            kind = 'removed'
            before = Math.max(0, before - 1)
          } else if (inHunk && line.startsWith(' ')) {
            before = Math.max(0, before - 1)
            after = Math.max(0, after - 1)
          }
        }
        const color = kind === 'added' ? 'bg-emerald-400/10 text-emerald-300'
          : kind === 'removed' ? 'bg-red-400/10 text-red-200' : ''
        return <span key={index} className={`block px-2.5 ${color}`}>{line}{index < lines.length - 1 ? '\n' : ''}</span>
      })}
    </pre>
  )
}
