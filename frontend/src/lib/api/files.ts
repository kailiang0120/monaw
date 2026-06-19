import { apiFetch, BASE } from './client'
import type { UploadedAttachment } from './types'

function fileUrl(attachment: Pick<UploadedAttachment, 'id'>, preview = false): string {
  const suffix = preview ? '/preview' : ''
  return `${BASE}/api/files/${encodeURIComponent(attachment.id)}${suffix}`
}

export async function fetchAttachmentObjectUrl(
  attachment: Pick<UploadedAttachment, 'id'>,
  preview = false,
): Promise<string> {
  const response = await apiFetch(fileUrl(attachment, preview))
  if (!response.ok) throw new Error('Failed to fetch attachment')
  return URL.createObjectURL(await response.blob())
}

export async function downloadAttachment(
  attachment: Pick<UploadedAttachment, 'id' | 'name'>,
): Promise<void> {
  const objectUrl = await fetchAttachmentObjectUrl(attachment)
  try {
    const anchor = document.createElement('a')
    anchor.href = objectUrl
    anchor.download = attachment.name
    anchor.rel = 'noreferrer'
    anchor.click()
  } finally {
    window.setTimeout(() => URL.revokeObjectURL(objectUrl), 1_000)
  }
}
