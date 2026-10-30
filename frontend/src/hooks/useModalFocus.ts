import { useEffect, useRef, type RefObject } from 'react'

const FOCUSABLE = [
  'a[href]',
  'button:not([disabled])',
  'input:not([disabled]):not([type="hidden"])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(',')

/** Open dialogs in the order they opened. Only the topmost one handles keys. */
const openDialogs: HTMLElement[] = []

/**
 * A dialog is on top unless it contains another open dialog, or a dialog
 * opened after it sits outside it. Containment matters because nested
 * dialogs that mount together register inner-first.
 */
function isTopmost(dialog: HTMLElement): boolean {
  if (openDialogs.some((other) => other !== dialog && dialog.contains(other))) return false
  const later = openDialogs.slice(openDialogs.indexOf(dialog) + 1)
  return !later.some((other) => !other.contains(dialog))
}

function focusables(dialog: HTMLElement): HTMLElement[] {
  return Array.from(dialog.querySelectorAll<HTMLElement>(FOCUSABLE))
}

/** Focus the first candidate that can take focus; hidden ones (e.g. display: none) cannot. */
function focusFirstAvailable(candidates: HTMLElement[]): boolean {
  for (const candidate of candidates) {
    candidate.focus()
    if (document.activeElement === candidate) return true
  }
  return false
}

interface Options {
  /** Called for Escape; the dialog decides whether closing needs confirmation. */
  onEscape: () => void
  /** Focused when the dialog opens; defaults to the first focusable control. */
  initialFocusRef?: RefObject<HTMLElement | null>
  /** Change this when the dialog element is replaced, e.g. a loading view becoming the dialog. */
  remountKey?: unknown
}

/**
 * Modal keyboard behaviour: initial focus, Tab kept inside the dialog, Escape
 * routed to the dialog, and focus returned to where it was when the dialog closes.
 *
 * Menus rendered in portals (such as `Dropdown`) sit outside the dialog in the
 * DOM. They handle their own Tab and Escape and stop Escape from reaching here.
 */
export function useModalFocus(dialogRef: RefObject<HTMLElement | null>, { onEscape, initialFocusRef, remountKey }: Options) {
  const onEscapeRef = useRef(onEscape)
  onEscapeRef.current = onEscape

  useEffect(() => {
    const dialog = dialogRef.current
    if (!dialog) return
    const active = document.activeElement instanceof HTMLElement ? document.activeElement : null
    const previous = active && !dialog.contains(active) ? active : null
    openDialogs.push(dialog)

    // A dialog nested inside this one may already have placed focus.
    if (!dialog.contains(document.activeElement)) {
      const initial = initialFocusRef?.current
      if (!(initial && focusFirstAvailable([initial])) && !focusFirstAvailable(focusables(dialog))) {
        dialog.focus()
      }
    }

    const onKeyDown = (event: KeyboardEvent) => {
      if (!isTopmost(dialog)) return
      if (event.key === 'Escape') {
        // A control inside the dialog (a picker, an inline edit) already used this Escape.
        if (event.defaultPrevented) return
        event.preventDefault()
        onEscapeRef.current()
        return
      }
      if (event.key !== 'Tab') return
      const focused = document.activeElement as HTMLElement | null
      if (focused?.closest('[role="listbox"]') && !dialog.contains(focused)) return
      const controls = focusables(dialog)
      if (controls.length === 0) {
        event.preventDefault()
        dialog.focus()
        return
      }
      const index = focused ? controls.indexOf(focused) : -1
      const outside = index < 0
      if (event.shiftKey && (outside || index === 0)) {
        event.preventDefault()
        focusFirstAvailable([...controls].reverse())
      } else if (!event.shiftKey && (outside || index === controls.length - 1)) {
        event.preventDefault()
        focusFirstAvailable(controls)
      }
    }

    // Bubble phase on window: controls inside the dialog and capture-phase menus act first.
    window.addEventListener('keydown', onKeyDown)
    return () => {
      window.removeEventListener('keydown', onKeyDown)
      const position = openDialogs.lastIndexOf(dialog)
      if (position >= 0) openDialogs.splice(position, 1)
      if (previous?.isConnected) previous.focus()
    }
    // The dialog element and initial focus change only with remountKey.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [remountKey])
}
