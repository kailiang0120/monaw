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

async function requestControlSession(forceRefresh = false): Promise<ControlSession> {
  if (!forceRefresh && controlSession && controlSession.expiresAt > Date.now() + 15_000) {
    return controlSession
  }
  if (!forceRefresh && controlSessionRequest) return controlSessionRequest

  controlSessionRequest = (async () => {
    const electronSession = await window.electronAPI?.getControlSession?.()
    const session = electronSession ?? developmentSession()
    if (!session?.token) {
      throw new Error(
        'Control-plane authentication is unavailable. Start Monaw through Electron or configure VITE_MONAW_CONTROL_TOKEN for browser development.',
      )
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
