import runtimeConfig from '../../../config/runtime.json'

const fallbackBase = `http://${runtimeConfig.backendHost || '127.0.0.1'}:${runtimeConfig.backendPort || 8420}`

function trimTrailingSlash(value: string): string {
  return value.replace(/\/+$/, '')
}

function resolveApiBase(): string {
  const envBase = String(import.meta.env.VITE_API_BASE_URL || '').trim()
  if (envBase) return trimTrailingSlash(envBase)

  if (typeof window !== 'undefined') {
    const electronBase = String(window.electronAPI?.backendBaseUrl || '').trim()
    if (electronBase) return trimTrailingSlash(electronBase)
  }

  return fallbackBase
}

export const BASE = resolveApiBase()

export const JSON_HEADERS = { 'Content-Type': 'application/json' } as const

export type ApiErrorEnvelope = {
  code: string
  message: string
  request_id: string
  details: Record<string, unknown>
}

export class ApiError extends Error {
  status: number
  code: string
  requestId: string
  details: Record<string, unknown>

  constructor(response: Response, envelope: ApiErrorEnvelope, fallbackMessage: string) {
    super(envelope.message || fallbackMessage)
    this.name = 'ApiError'
    this.status = response.status
    this.code = envelope.code || 'request_failed'
    this.requestId = envelope.request_id || response.headers.get('x-request-id') || ''
    this.details = envelope.details || {}
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === 'object' && !Array.isArray(value))
}

function legacyMessage(payload: Record<string, unknown>, fallbackMessage: string): string {
  const detail = payload.detail
  if (typeof detail === 'string' && detail) return detail
  if (isRecord(detail) && typeof detail.message === 'string' && detail.message) {
    return detail.message
  }
  return fallbackMessage
}

export async function decodeApiError(response: Response, fallbackMessage = 'Request failed'): Promise<ApiError> {
  const payload = await response.json().catch(() => null)
  const record = isRecord(payload) ? payload : {}
  const envelope: ApiErrorEnvelope = {
    code: typeof record.code === 'string' && record.code ? record.code : 'request_failed',
    message: typeof record.message === 'string' && record.message ? record.message : legacyMessage(record, fallbackMessage),
    request_id: typeof record.request_id === 'string' ? record.request_id : response.headers.get('x-request-id') || '',
    details: isRecord(record.details) ? record.details : {},
  }
  return new ApiError(response, envelope, fallbackMessage)
}

export async function throwApiError(response: Response, fallbackMessage = 'Request failed'): Promise<never> {
  throw await decodeApiError(response, fallbackMessage)
}

type ControlSession = {
  token: string
  expiresAt: number
}

let controlSession: ControlSession | null = null
let controlSessionRequest: Promise<ControlSession> | null = null

function developmentSession(): ControlSession | null {
  const token = String(import.meta.env.VITE_MONAW_CONTROL_TOKEN || '').trim()
  return token ? { token, expiresAt: Number.MAX_SAFE_INTEGER } : null
}

function authenticationUnavailableMessage(): string {
  const isElectronRuntime = (
    typeof navigator !== 'undefined' &&
    navigator.userAgent.toLowerCase().includes('electron')
  )
  return isElectronRuntime
    ? 'The desktop authentication bridge failed to initialize. Restart Monaw and check the launcher logs if the problem continues.'
    : 'This page is running outside the Monaw desktop app. Start Monaw with start.bat.'
}

async function requestControlSession(forceRefresh = false): Promise<ControlSession> {
  if (!forceRefresh && controlSession && controlSession.expiresAt > Date.now() + 15_000) {
    return controlSession
  }
  if (!forceRefresh && controlSessionRequest) return controlSessionRequest

  controlSessionRequest = (async () => {
    const electronSession = await window.electronAPI?.getControlSession?.()
    const session = electronSession ?? developmentSession()
    if (!session?.token) {
      throw new Error(authenticationUnavailableMessage())
    }
    controlSession = session
    return session
  })()

  try {
    return await controlSessionRequest
  } finally {
    controlSessionRequest = null
  }
}

export async function apiFetch(
  input: RequestInfo | URL,
  init: RequestInit = {},
  retryOnUnauthorized = true,
): Promise<Response> {
  const session = await requestControlSession()
  const headers = new Headers(init.headers)
  headers.set('Authorization', `Bearer ${session.token}`)
  const response = await fetch(input, { ...init, headers })

  if (response.status === 401 && retryOnUnauthorized) {
    controlSession = null
    const refreshed = await requestControlSession(true)
    const retryHeaders = new Headers(init.headers)
    retryHeaders.set('Authorization', `Bearer ${refreshed.token}`)
    return fetch(input, { ...init, headers: retryHeaders })
  }
  return response
}

export function resetApiSessionForTests(): void {
  controlSession = null
  controlSessionRequest = null
}
