import { useEffect, useRef } from 'react'

interface Props {
  /** The question, read by assistive technology, e.g. `Delete "Weekly report"?` */
  label: string
  confirmLabel?: string
  onConfirm: () => void
  onCancel: () => void
  className?: string
}

/**
 * A compact inline confirmation for an irreversible action. Focus starts on
 * Cancel so the destructive choice is never the default, and Escape cancels.
 */
export function InlineConfirm({ label, confirmLabel = 'Delete', onConfirm, onCancel, className = '' }: Props) {
  const cancelRef = useRef<HTMLButtonElement>(null)

  useEffect(() => {
    cancelRef.current?.focus()
  }, [])

  return (
    <div
      role="group"
      aria-label={label}
      className={`flex shrink-0 items-center gap-0.5 ${className}`}
      onClick={(event) => event.stopPropagation()}
      onKeyDown={(event) => {
        if (event.key !== 'Escape') return
        event.preventDefault()
        event.stopPropagation()
        onCancel()
      }}
    >
      <button
        type="button"
        onClick={onConfirm}
        className="rounded-md px-1.5 py-0.5 text-[10px] font-medium text-red-300 transition-colors hover:bg-red-500/10 focus:outline-none focus-visible:ring-2 focus-visible:ring-red-400/40"
      >
        {confirmLabel}
      </button>
      <button
        ref={cancelRef}
        type="button"
        onClick={onCancel}
        className="rounded-md px-1.5 py-0.5 text-[10px] text-neutral-400 transition-colors hover:bg-white/[0.06] hover:text-neutral-200 focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/30"
      >
        Cancel
      </button>
    </div>
  )
}
