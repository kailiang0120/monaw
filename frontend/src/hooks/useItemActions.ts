import { useEffect, useRef, useState } from 'react'
import { ApiError } from '../lib/api/client'

export type ItemActionFeedback = {
  pending?: string
  error?: string
  success?: string
  retry?: () => void
  retryLabel?: string
}

export function actionErrorMessage(error: unknown, fallback: string): string {
  const message = error instanceof Error ? error.message : fallback
  const safe = message
    .replace(/Bearer\s+\S+/gi, 'Bearer [redacted]')
    .replace(/((?:token|api[_-]?key|secret|password)\s*[=:]\s*)[^\s&,]+/gi, '$1[redacted]')
    .replace(/(https?:\/\/)[^\s/@]+:[^\s/@]+@/gi, '$1[redacted]@')
    .replace(/[\u0000-\u001f]/g, ' ')
    .slice(0, 300)
  return error instanceof ApiError && error.requestId
    ? `${safe} (Reference: ${error.requestId.slice(0, 80)})`
    : safe
}

export function useItemActions(invalidate: () => void) {
  const [feedback, setFeedback] = useState<Record<string, ItemActionFeedback>>({})
  const busy = useRef(new Set<string>())
  const mounted = useRef(true)
  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false }
  }, [])

  async function perform<T>(
    key: string, label: string, operation: () => Promise<T>, commit: (value: T) => void,
    retry?: () => void, success = 'Saved', retryLabel = 'Retry',
  ): Promise<T | undefined> {
    if (busy.current.has(key)) return undefined
    busy.current.add(key)
    invalidate()
    setFeedback((state) => ({ ...state, [key]: { pending: label } }))
    try {
      const value = await operation()
      if (!mounted.current) return undefined
      invalidate()
      commit(value)
      setFeedback((state) => ({ ...state, [key]: { success } }))
      return value
    } catch (error) {
      if (mounted.current) {
        invalidate()
        setFeedback((state) => ({ ...state, [key]: { error: actionErrorMessage(error, `${label} failed`), retry, retryLabel } }))
      }
      return undefined
    } finally {
      busy.current.delete(key)
    }
  }

  return { feedback, perform }
}
