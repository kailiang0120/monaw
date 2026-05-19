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
