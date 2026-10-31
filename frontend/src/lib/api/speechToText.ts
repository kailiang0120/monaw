import { apiFetch, BASE, JSON_HEADERS, decodeApiError } from './client'
import type { SpeechToTextTranscription } from './types'

const WAV_SAMPLE_RATE = 16_000

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

/** 16-bit PCM WAV bytes for mono samples in [-1, 1]. */
export function encodeWav(samples: Float32Array, sampleRate: number): ArrayBuffer {
  const buffer = new ArrayBuffer(44 + samples.length * 2)
  const view = new DataView(buffer)
  const writeText = (offset: number, text: string) => {
    for (let index = 0; index < text.length; index += 1) view.setUint8(offset + index, text.charCodeAt(index))
  }
  writeText(0, 'RIFF')
  view.setUint32(4, 36 + samples.length * 2, true)
  writeText(8, 'WAVE')
  writeText(12, 'fmt ')
  view.setUint32(16, 16, true)
  view.setUint16(20, 1, true)
  view.setUint16(22, 1, true)
  view.setUint32(24, sampleRate, true)
  view.setUint32(28, sampleRate * 2, true)
  view.setUint16(32, 2, true)
  view.setUint16(34, 16, true)
  writeText(36, 'data')
  view.setUint32(40, samples.length * 2, true)
  samples.forEach((sample, index) => {
    const clamped = Math.max(-1, Math.min(1, sample))
    view.setInt16(44 + index * 2, clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff, true)
  })
  return buffer
}

// The recorder produces WebM, which Gemini does not accept. Decode it here and
// upload 16 kHz mono WAV, which both OpenAI and Gemini take.
async function recordingToWav(blob: Blob): Promise<Blob> {
  if (blob.type.startsWith('audio/wav')) return blob
  const AudioContextCtor = window.AudioContext || (window as typeof window & { webkitAudioContext?: typeof AudioContext }).webkitAudioContext
  if (!AudioContextCtor || typeof OfflineAudioContext === 'undefined') return blob
  try {
    const context = new AudioContextCtor()
    let decoded: AudioBuffer
    try {
      decoded = await context.decodeAudioData(await blob.arrayBuffer())
    } finally {
      void context.close()
    }
    const offline = new OfflineAudioContext(1, Math.max(1, Math.ceil(decoded.duration * WAV_SAMPLE_RATE)), WAV_SAMPLE_RATE)
    const source = offline.createBufferSource()
    source.buffer = decoded
    source.connect(offline.destination)
    source.start()
    const rendered = await offline.startRendering()
    return new Blob([encodeWav(rendered.getChannelData(0), WAV_SAMPLE_RATE)], { type: 'audio/wav' })
  } catch (error) {
    console.warn('[speech-to-text] could not convert recording to WAV; uploading it unchanged', {
      type: blob.type || 'unknown',
      error: error instanceof Error ? error.message : String(error),
    })
    return blob
  }
}

function audioExtension(type: string): string {
  if (type.includes('wav')) return 'wav'
  if (type.includes('mp4')) return 'm4a'
  if (type.includes('ogg')) return 'ogg'
  return 'webm'
}

export async function transcribeSpeech(recording: Blob): Promise<SpeechToTextTranscription> {
  const blob = await recordingToWav(recording)
  console.info('[speech-to-text] upload start', {
    size: blob.size,
    type: blob.type || 'unknown',
    recordedType: recording.type || 'unknown',
  })
  const dataBase64 = await blobToBase64(blob)
  const res = await apiFetch(`${BASE}/api/speech-to-text/transcribe`, {
    method: 'POST',
    headers: JSON_HEADERS,
    body: JSON.stringify({
      filename: `voice-input.${audioExtension(blob.type)}`,
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
