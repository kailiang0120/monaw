import { type KeyboardEvent as ReactKeyboardEvent, type ReactNode, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { Check, ChevronDown } from 'lucide-react'

export interface DropdownOption<T extends string> {
  value: T
  label: string
  /** Shown on the closed control instead of `label` when space is tight; the menu keeps `label`. */
  shortLabel?: string
}

interface Props<T extends string> {
  value: T
  options: DropdownOption<T>[]
  onChange: (value: T) => void
  disabled?: boolean
  ariaLabel?: string
  placeholder?: string
  align?: 'left' | 'right'
  size?: 'sm' | 'md'
  className?: string
  icon?: ReactNode
}

const MENU_MARGIN = 4
const MIN_MENU_SPACE = 180

export function Dropdown<T extends string>({
  value,
  options,
  onChange,
  disabled,
  ariaLabel,
  placeholder,
  align = 'left',
  size = 'md',
  className = '',
  icon,
}: Props<T>) {
  const [open, setOpen] = useState(false)
  const [menuStyle, setMenuStyle] = useState<{ top: number; left: number; minWidth: number } | null>(null)
  const triggerRef = useRef<HTMLButtonElement>(null)
  const menuRef = useRef<HTMLDivElement>(null)
  const active = options.find((option) => option.value === value)

  /**
   * Portal into the theme root rather than <body>, so the menu still inherits
   * the theme's CSS variables. Neither element establishes a containing block,
   * so `position: fixed` resolves against the viewport either way.
   */
  const portalContainer = (): HTMLElement => {
    const themeRoot = triggerRef.current?.closest('.theme-dark, .theme-light')
    return (themeRoot as HTMLElement | null) ?? document.body
  }

  /**
   * The menu renders in a portal with fixed positioning. Anchoring it to the
   * trigger inside the DOM would let any ancestor with `overflow: hidden` (a
   * settings card, the modal shell) clip it.
   */
  useLayoutEffect(() => {
    if (!open) return
    const trigger = triggerRef.current
    if (!trigger) return

    const place = () => {
      const rect = trigger.getBoundingClientRect()
      const spaceBelow = window.innerHeight - rect.bottom
      const menuHeight = menuRef.current?.offsetHeight ?? 0
      const dropUp = spaceBelow < Math.min(MIN_MENU_SPACE, menuHeight + MENU_MARGIN)
      const top = dropUp
        ? Math.max(MENU_MARGIN, rect.top - menuHeight - MENU_MARGIN)
        : rect.bottom + MENU_MARGIN
      const left = align === 'right'
        ? Math.max(MENU_MARGIN, rect.right - (menuRef.current?.offsetWidth ?? rect.width))
        : rect.left
      setMenuStyle({ top, left, minWidth: rect.width })
    }

    place()
    window.addEventListener('scroll', place, true)
    window.addEventListener('resize', place)
    return () => {
      window.removeEventListener('scroll', place, true)
      window.removeEventListener('resize', place)
    }
  }, [open, align, options.length])

  const close = ({ restoreFocus }: { restoreFocus: boolean }) => {
    setOpen(false)
    if (restoreFocus) triggerRef.current?.focus()
  }

  useEffect(() => {
    if (!open) return
    const handlePointerDown = (event: MouseEvent) => {
      const target = event.target as Node
      if (triggerRef.current?.contains(target)) return
      if (menuRef.current?.contains(target)) return
      setOpen(false)
    }
    const handleKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        // Close only the menu, not a dialog that contains the trigger.
        event.stopPropagation()
        close({ restoreFocus: true })
      }
    }
    window.addEventListener('mousedown', handlePointerDown)
    window.addEventListener('keydown', handleKey, true)
    return () => {
      window.removeEventListener('mousedown', handlePointerDown)
      window.removeEventListener('keydown', handleKey, true)
    }
  }, [open])

  const optionButtons = () => Array.from(menuRef.current?.querySelectorAll<HTMLButtonElement>('[role="option"]') ?? [])

  // Once placed (a hidden menu cannot take focus), focus the selected option, or the requested end.
  const pendingFocus = useRef<'selected' | 'first' | 'last'>('selected')
  const placed = menuStyle !== null
  useEffect(() => {
    if (!open || !placed) return
    const buttons = optionButtons()
    const selectedIndex = options.findIndex((option) => option.value === value)
    const index = pendingFocus.current === 'last'
      ? buttons.length - 1
      : pendingFocus.current === 'first' || selectedIndex < 0 ? 0 : selectedIndex
    buttons[index]?.focus()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, placed])

  useEffect(() => {
    if (!open) setMenuStyle(null)
  }, [open])

  const openWith = (focus: 'selected' | 'first' | 'last') => {
    pendingFocus.current = focus
    setOpen(true)
  }

  const handleTriggerKeyDown = (event: ReactKeyboardEvent<HTMLButtonElement>) => {
    if (open) return
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault()
      openWith(event.key === 'ArrowUp' && !active ? 'last' : 'selected')
    }
  }

  const handleMenuKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    const buttons = optionButtons()
    if (buttons.length === 0) return
    const current = buttons.indexOf(document.activeElement as HTMLButtonElement)
    const move = (index: number) => {
      event.preventDefault()
      buttons[(index + buttons.length) % buttons.length].focus()
    }
    if (event.key === 'ArrowDown') move(current + 1)
    else if (event.key === 'ArrowUp') move(current < 0 ? buttons.length - 1 : current - 1)
    else if (event.key === 'Home') move(0)
    else if (event.key === 'End') move(buttons.length - 1)
    else if (event.key === 'Tab') close({ restoreFocus: true })
  }

  const menu = open && (
    <div
      ref={menuRef}
      role="listbox"
      aria-label={ariaLabel}
      className="st-menu"
      onKeyDown={handleMenuKeyDown}
      style={{
        position: 'fixed',
        top: menuStyle?.top ?? -9999,
        left: menuStyle?.left ?? -9999,
        minWidth: menuStyle?.minWidth,
        visibility: menuStyle ? 'visible' : 'hidden',
      }}
    >
      {options.map((option) => {
        const selected = option.value === value
        return (
          <button
            key={option.value}
            type="button"
            role="option"
            aria-selected={selected}
            tabIndex={-1}
            onClick={() => {
              onChange(option.value)
              close({ restoreFocus: true })
            }}
            className="st-menu-item"
          >
            <span className="truncate">{option.label}</span>
            {selected && <Check size={12} className="shrink-0" />}
          </button>
        )
      })}
    </div>
  )

  return (
    <div className={`relative ${className}`}>
      <button
        ref={triggerRef}
        type="button"
        disabled={disabled}
        onClick={() => {
          pendingFocus.current = 'selected'
          setOpen((current) => !current)
        }}
        onKeyDown={handleTriggerKeyDown}
        aria-label={ariaLabel}
        aria-haspopup="listbox"
        aria-expanded={open}
        title={active?.shortLabel ? active.label : undefined}
        className={`st-select ${size === 'sm' ? 'st-select-sm' : ''}`}
      >
        <span className="flex min-w-0 items-center gap-1.5">
          {icon}
          <span className="truncate">{active?.shortLabel ?? active?.label ?? placeholder ?? 'Select'}</span>
        </span>
        <ChevronDown size={12} className={`shrink-0 transition-transform ${open ? 'rotate-180' : ''}`} />
      </button>
      {menu ? createPortal(menu, portalContainer()) : null}
    </div>
  )
}
