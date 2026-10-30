import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { Download, Image, Loader2, X } from 'lucide-react'
import { useModalFocus } from '../hooks/useModalFocus'
import { downloadAttachment, fetchAttachmentObjectUrl } from '../lib/api/files'
import type { UploadedAttachment } from '../lib/api/types'

/** In-app lightbox for image attachments, so chat never opens a second window. */
export function ImagePreviewDialog({
  attachment,
  onClose,
}: {
  attachment: UploadedAttachment
  onClose: () => void
}) {
  const [src, setSrc] = useState('')
  const [failed, setFailed] = useState(false)
  const closeRef = useRef<HTMLButtonElement>(null)
  const dialogRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    let active = true
    let objectUrl = ''
    setSrc('')
    setFailed(false)
    void fetchAttachmentObjectUrl(attachment)
      .then((value) => {
        objectUrl = value
        if (active) setSrc(value)
        else URL.revokeObjectURL(value)
      })
      .catch(() => {
        if (active) setFailed(true)
      })
    return () => {
      active = false
      if (objectUrl) URL.revokeObjectURL(objectUrl)
    }
  }, [attachment])

  useModalFocus(dialogRef, { onEscape: onClose, initialFocusRef: closeRef })

  // Portal out of the message list: animated (transformed) ancestors would
  // otherwise trap the fixed overlay inside the bubble.
  const container = document.querySelector('.theme-dark, .theme-light') ?? document.body
  return createPortal(
    <div
      className="st-overlay fixed inset-0 below-app-bar z-50 flex items-center justify-center p-6"
      onClick={onClose}
    >
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-label={`Image preview: ${attachment.name}`}
        tabIndex={-1}
        className="settings-shell flex max-h-full max-w-full flex-col overflow-hidden rounded-2xl"
        onClick={(event) => event.stopPropagation()}
      >
        <header className="st-divider-b flex items-center justify-between gap-3 px-4 py-2.5">
          <h2 className="st-title min-w-0 truncate" title={attachment.name}>{attachment.name}</h2>
          <div className="flex shrink-0 items-center gap-1">
            <button
              type="button"
              onClick={() => void downloadAttachment(attachment)}
              className="st-btn st-btn-ghost st-btn-icon"
              aria-label="Download image"
              title="Download image"
            >
              <Download size={15} />
            </button>
            <button
              ref={closeRef}
              type="button"
              onClick={onClose}
              className="st-btn st-btn-ghost st-btn-icon"
              aria-label="Close preview"
            >
              <X size={15} />
            </button>
          </div>
        </header>
        <div className="flex min-h-[200px] min-w-[280px] items-center justify-center overflow-auto bg-black/25 p-2">
          {!failed && src ? (
            <img
              src={src}
              alt={attachment.name}
              className="max-h-[calc(100vh-10rem)] max-w-[calc(100vw-5rem)] object-contain"
              onError={() => setFailed(true)}
            />
          ) : failed ? (
            <span className="flex flex-col items-center gap-2 text-xs text-neutral-400">
              <Image size={24} className="text-neutral-500" aria-hidden />
              Preview unavailable
            </span>
          ) : (
            <Loader2 size={20} className="animate-spin text-neutral-500" aria-label="Loading image" />
          )}
        </div>
      </div>
    </div>,
    container,
  )
}
