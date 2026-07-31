import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { Check, ChevronDown } from 'lucide-react'

export interface DropdownOption<T extends string> {
  value: T
  label: string
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
        event.stopPropagation()
        setOpen(false)
      }
    }
    window.addEventListener('mousedown', handlePointerDown)
    window.addEventListener('keydown', handleKey, true)
    return () => {
      window.removeEventListener('mousedown', handlePointerDown)
      window.removeEventListener('keydown', handleKey, true)
    }
  }, [open])

  const menu = open && (
    <div
      ref={menuRef}
      role="listbox"
      aria-label={ariaLabel}
      className="st-menu"
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
            onClick={() => {
              onChange(option.value)
              setOpen(false)
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
        onClick={() => setOpen((current) => !current)}
        aria-label={ariaLabel}
        aria-haspopup="listbox"
        aria-expanded={open}
        className={`st-select ${size === 'sm' ? 'st-select-sm' : ''}`}
      >
        <span className="truncate">{active?.label ?? placeholder ?? 'Select'}</span>
        <ChevronDown size={12} className={`shrink-0 transition-transform ${open ? 'rotate-180' : ''}`} />
      </button>
      {menu ? createPortal(menu, portalContainer()) : null}
    </div>
  )
}
