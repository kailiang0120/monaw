import { apiFetch, BASE, JSON_HEADERS, decodeApiError } from './client'
import type { SpeechToTextTranscription } from './types'

function blobToBase64(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => {
      const value = String(reader.result ?? '')
      resolve(value.includes(',') ? value.split(',', 2)[1] : value)
    }
    reader.onerror = () => reject(reader.error ?? new Error('Unable to read audio'))
    reader.readAsDataURL(blob)
  })
}

export async function transcribeSpeech(blob: Blob): Promise<SpeechToTextTranscription> {
  console.info('[speech-to-text] upload start', {
    size: blob.size,
    type: blob.type || 'unknown',
  })
  const dataBase64 = await blobToBase64(blob)
  const extension = blob.type.includes('mp4') ? 'm4a' : blob.type.includes('ogg') ? 'ogg' : 'webm'
  const res = await apiFetch(`${BASE}/api/speech-to-text/transcribe`, {
    method: 'POST',
    headers: JSON_HEADERS,
    body: JSON.stringify({
      filename: `voice-input.${extension}`,
      mime_type: blob.type || 'audio/webm',
      data_base64: dataBase64,
    }),
  })
  if (!res.ok) {
    const error = await decodeApiError(res, 'Speech-to-text failed')
    console.error('[speech-to-text] upload failed', {
      status: res.status,
      code: error.code,
      requestId: error.requestId,
      message: error.message,
      size: blob.size,
      type: blob.type || 'unknown',
    })
    throw error
  }
  const result = await res.json()
  console.info('[speech-to-text] upload complete', {
    textChars: String(result.text || '').length,
    size: blob.size,
    type: blob.type || 'unknown',
  })
  return result
}
