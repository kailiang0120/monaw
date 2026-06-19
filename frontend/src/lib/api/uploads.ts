import { apiFetch, BASE, JSON_HEADERS } from './client'
import type { UploadedAttachment } from './types'

function fileToBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => {
      const value = String(reader.result ?? '')
      resolve(value.includes(',') ? value.split(',', 2)[1] : value)
    }
    reader.onerror = () => reject(reader.error ?? new Error('Unable to read file'))
    reader.readAsDataURL(file)
  })
}

export async function uploadAttachment(
  file: File,
  conversationId: string | null,
): Promise<UploadedAttachment> {
  const dataBase64 = await fileToBase64(file)
  const res = await apiFetch(`${BASE}/api/uploads`, {
    method: 'POST',
    headers: JSON_HEADERS,
    body: JSON.stringify({
      filename: file.name,
      mime_type: file.type,
      size: file.size,
      conversation_id: conversationId,
      data_base64: dataBase64,
    }),
  })

  if (!res.ok) {
    const detail = await res.text().catch(() => '')
    throw new Error(detail || 'Upload failed')
  }

  return res.json()
}
