import { apiFetch, BASE } from './client'

export interface ServerEvent {
  id?: number
  event: string
  data: Record<string, unknown>
}

interface SubscribeOptions {
  onEvent: (event: ServerEvent) => void
  onStatus?: (connected: boolean) => void
}

const RECONNECT_DELAY_MS = 2500

export function subscribeServerEvents({ onEvent, onStatus }: SubscribeOptions): () => void {
  let stopped = false
  let lastEventId: number | undefined
  let controller: AbortController | null = null

  const connect = async () => {
    while (!stopped) {
      controller = new AbortController()
      try {
        const headers = new Headers()
        if (lastEventId !== undefined) headers.set('Last-Event-ID', String(lastEventId))
        const response = await apiFetch(`${BASE}/api/events`, {
          headers,
          signal: controller.signal,
        })
        if (!response.ok || !response.body) {
          throw new Error(`Event stream unavailable (${response.status})`)
        }
        onStatus?.(true)
        await readEventStream(response.body, (event) => {
          if (event.id !== undefined) lastEventId = event.id
          onEvent(event)
        })
      } catch (error) {
        if (!stopped && !(error instanceof DOMException && error.name === 'AbortError')) {
          onStatus?.(false)
          await delay(RECONNECT_DELAY_MS)
        }
      } finally {
        controller = null
      }
    }
    onStatus?.(false)
  }

  void connect()

  return () => {
    stopped = true
    controller?.abort()
  }
}

async function readEventStream(
  body: ReadableStream<Uint8Array>,
  onEvent: (event: ServerEvent) => void,
): Promise<void> {
  const reader = body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  try {
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      let boundary = buffer.search(/\r?\n\r?\n/)
      while (boundary >= 0) {
        const frame = buffer.slice(0, boundary)
        buffer = buffer.slice(buffer[boundary] === '\r' ? boundary + 4 : boundary + 2)
        const parsed = parseFrame(frame)
        if (parsed) onEvent(parsed)
        boundary = buffer.search(/\r?\n\r?\n/)
      }
    }
  } finally {
    reader.releaseLock()
  }
}

function parseFrame(frame: string): ServerEvent | null {
  let event = 'message'
  let id: number | undefined
  const dataLines: string[] = []

  for (const rawLine of frame.split(/\r?\n/)) {
    const line = rawLine.trimEnd()
    if (!line || line.startsWith(':')) continue
    const separator = line.indexOf(':')
    const field = separator >= 0 ? line.slice(0, separator) : line
    const value = separator >= 0 ? line.slice(separator + 1).replace(/^ /, '') : ''
    if (field === 'event') event = value || 'message'
    if (field === 'id') {
      const parsed = Number(value)
      if (Number.isFinite(parsed)) id = parsed
    }
    if (field === 'data') dataLines.push(value)
  }

  if (!event && dataLines.length === 0) return null
  const rawData = dataLines.join('\n')
  let data: Record<string, unknown> = {}
  if (rawData) {
    try {
      const parsed = JSON.parse(rawData)
      data = parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : { value: parsed }
    } catch {
      data = { value: rawData }
    }
  }
  return { id, event, data }
}

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, ms))
}
