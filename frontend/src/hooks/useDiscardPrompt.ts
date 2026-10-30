import { useEffect, useRef, useState } from 'react'

/**
 * An inline "discard unsaved changes?" prompt that is usable from the keyboard:
 * asking moves focus to Keep editing, and keeping on editing returns focus to
 * where the user was.
 */
export function useDiscardPrompt() {
  const [confirming, setConfirming] = useState(false)
  const keepEditingRef = useRef<HTMLButtonElement>(null)
  const returnFocusRef = useRef<HTMLElement | null>(null)

  useEffect(() => {
    if (confirming) {
      keepEditingRef.current?.focus()
      return
    }
    const previous = returnFocusRef.current
    returnFocusRef.current = null
    if (previous?.isConnected) previous.focus()
  }, [confirming])

  const ask = () => {
    if (!confirming) {
      returnFocusRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null
    }
    setConfirming(true)
  }

  const keepEditing = () => setConfirming(false)

  return { confirming, ask, keepEditing, keepEditingRef }
}
