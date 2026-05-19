import { BASE } from './client'
import type { UploadedAttachment } from './types'

export function fileUrl(attachment: Pick<UploadedAttachment, 'id'>): string {
  return `${BASE}/api/files/${encodeURIComponent(attachment.id)}`
}

export function filePreviewUrl(attachment: Pick<UploadedAttachment, 'id'>): string {
  return `${fileUrl(attachment)}/preview`
}
