import { useState, type ReactNode } from 'react'

/** Fold lengthy historical text while keeping it searchable and keyboard accessible. */
export function FoldableMessage({
  content,
  eligible,
  children,
}: {
  content: string
  eligible: boolean
  children: ReactNode
}) {
  const [expanded, setExpanded] = useState(false)
  const isLong = content.length > 1600 || content.split('\n').length > 20
  if (!eligible || !isLong) return <>{children}</>

  const preview = content.trim().replace(/\s+/g, ' ').slice(0, 220)
  return (
    <details className="message-fold" open={expanded} onToggle={(event) => setExpanded(event.currentTarget.open)}>
      <summary className="cursor-pointer list-none rounded-md py-2 text-sm focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-current [&::-webkit-details-marker]:hidden">
        <span className="message-fold-label inline-flex items-center gap-1.5 font-medium">{expanded ? 'Show less' : 'Show full message'}</span>
        {!expanded && <span className="mt-1 block break-words text-sm font-normal">{preview}…</span>}
      </summary>
      <div className="pt-1">{children}</div>
    </details>
  )
}
