import { useEffect, useId, useRef } from 'react'
import '../../../electron/message-dialog.css'

interface Props {
  title: string
  message: string
  confirmLabel?: string
  cancelLabel?: string
  destructive?: boolean
  onClose: (confirmed: boolean) => void
}

/** Show themed confirmations and alerts with native modal focus and dismissal. */
export function MessageDialog({ title, message, confirmLabel = 'OK', cancelLabel, destructive = false, onClose }: Props) {
  const id = useId()
  const dialogRef = useRef<HTMLDialogElement>(null)

  useEffect(() => {
    const dialog = dialogRef.current
    if (dialog && !dialog.open) {
      dialog.returnValue = ''
      dialog.showModal()
    }
  }, [])

  /** Treat Escape and any other dismissal without an explicit choice as cancel. */
  const finish = () => {
    const dialog = dialogRef.current
    const confirmed = dialog?.returnValue === 'confirm'
    if (dialog) dialog.returnValue = ''
    onClose(confirmed)
  }

  return (
    <dialog ref={dialogRef} className="monaw-message" aria-labelledby={`${id}-title`} aria-describedby={`${id}-message`} onClose={finish}>
      <h1 id={`${id}-title`} className="monaw-message-title">{title}</h1>
      <p id={`${id}-message`} className="monaw-message-body">{message}</p>
      <div className="monaw-message-actions">
        {cancelLabel && (
          <button type="button" className="monaw-message-button" autoFocus onClick={() => dialogRef.current?.close('cancel')}>
            {cancelLabel}
          </button>
        )}
        <button
          type="button"
          className={`monaw-message-button ${destructive ? 'monaw-message-danger' : 'monaw-message-primary'}`}
          autoFocus={!cancelLabel}
          onClick={() => dialogRef.current?.close('confirm')}
        >
          {confirmLabel}
        </button>
      </div>
    </dialog>
  )
}
