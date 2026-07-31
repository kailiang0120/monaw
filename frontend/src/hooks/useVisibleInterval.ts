import { useEffect, useRef } from 'react'

export function useVisibleInterval(callback: () => void, delayMs: number | null): void {
  const callbackRef = useRef(callback)

  useEffect(() => {
    callbackRef.current = callback
  }, [callback])

  useEffect(() => {
    if (delayMs === null) return

    let timer: number | null = null
    const clear = () => {
      if (timer !== null) {
        window.clearInterval(timer)
        timer = null
      }
    }
    const start = () => {
      if (document.visibilityState === 'hidden' || timer !== null) return
      timer = window.setInterval(() => callbackRef.current(), delayMs)
    }
    const handleVisibility = () => {
      if (document.visibilityState === 'hidden') {
        clear()
        return
      }
      callbackRef.current()
      start()
    }

    start()
    document.addEventListener('visibilitychange', handleVisibility)
    return () => {
      clear()
      document.removeEventListener('visibilitychange', handleVisibility)
    }
  }, [delayMs])
}
