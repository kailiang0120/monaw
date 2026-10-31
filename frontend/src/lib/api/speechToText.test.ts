import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { resetApiSessionForTests } from './client'
import { encodeWav, transcribeSpeech } from './speechToText'

describe('speech-to-text upload', () => {
  beforeEach(() => {
    ;(window as any).electronAPI = {
      getControlSession: vi.fn().mockResolvedValue({ token: 'session', expiresAt: Date.now() + 60_000 }),
    }
  })

  afterEach(() => {
    resetApiSessionForTests()
    delete (window as any).electronAPI
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('encodes mono 16-bit PCM WAV', () => {
    const view = new DataView(encodeWav(new Float32Array([0, 1, -1, 2]), 16_000))
    const text = (offset: number) => String.fromCharCode(...new Uint8Array(view.buffer, offset, 4))

    expect([text(0), text(8), text(12), text(36)]).toEqual(['RIFF', 'WAVE', 'fmt ', 'data'])
    expect(view.getUint16(22, true)).toBe(1)
    expect(view.getUint32(24, true)).toBe(16_000)
    expect(view.getUint16(34, true)).toBe(16)
    expect(view.getUint32(40, true)).toBe(8)
    expect([44, 46, 48, 50].map((offset) => view.getInt16(offset, true))).toEqual([0, 32767, -32768, 32767])
  })

  it('uploads the recording as WAV after decoding it', async () => {
    const decoded = { duration: 0.5 }
    const close = vi.fn().mockResolvedValue(undefined)
    vi.stubGlobal('AudioContext', vi.fn(function AudioContext() {
      return { decodeAudioData: vi.fn().mockResolvedValue(decoded), close }
    }))
    vi.stubGlobal('OfflineAudioContext', vi.fn(function OfflineAudioContext(channels: number, length: number, rate: number) {
      expect([channels, length, rate]).toEqual([1, 8_000, 16_000])
      return {
        destination: {},
        createBufferSource: () => ({ connect: vi.fn(), start: vi.fn(), buffer: null }),
        startRendering: vi.fn().mockResolvedValue({ getChannelData: () => new Float32Array(8_000) }),
      }
    }))
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ text: 'hello' }), { status: 200 }),
    )

    const result = await transcribeSpeech(new Blob(['webm'], { type: 'audio/webm;codecs=opus' }))

    expect(result.text).toBe('hello')
    expect(close).toHaveBeenCalled()
    const body = JSON.parse(String(fetchMock.mock.calls[0][1]?.body))
    expect(body.filename).toBe('voice-input.wav')
    expect(body.mime_type).toBe('audio/wav')
    expect(atob(body.data_base64)).toHaveLength(44 + 16_000)
  })

  it('uploads the original recording when it cannot be decoded', async () => {
    vi.stubGlobal('AudioContext', vi.fn(function AudioContext() {
      return { decodeAudioData: vi.fn().mockRejectedValue(new Error('unsupported')), close: vi.fn().mockResolvedValue(undefined) }
    }))
    vi.stubGlobal('OfflineAudioContext', vi.fn())
    vi.spyOn(console, 'warn').mockImplementation(() => {})
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ text: '' }), { status: 200 }),
    )

    await transcribeSpeech(new Blob(['ogg'], { type: 'audio/ogg;codecs=opus' }))

    const body = JSON.parse(String(fetchMock.mock.calls[0][1]?.body))
    expect(body.filename).toBe('voice-input.ogg')
    expect(body.mime_type).toBe('audio/ogg;codecs=opus')
  })
})
