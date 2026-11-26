import { type KeyboardEvent as ReactKeyboardEvent, type ReactNode, useEffect, useId, useLayoutEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { Check, ChevronDown } from 'lucide-react'

export interface DropdownOption<T extends string> {
  value: T
  label: string
  /** Shown on the closed control instead of `label` when space is tight; the menu keeps `label`. */
  shortLabel?: string
  group?: string
  disabled?: boolean
  alwaysVisible?: boolean
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
  id?: string
  title?: string
  ariaDescribedBy?: string
  searchable?: boolean
  searchLabel?: string
}

const MENU_MARGIN = 4

/** Share themed selection menus, keyboard navigation, and optional filtering. */
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
  id,
  title,
  ariaDescribedBy,
  searchable = false,
  searchLabel = 'Search options',
}: Props<T>) {
  const menuId = useId()
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const [menuStyle, setMenuStyle] = useState<{ top: number; left: number; minWidth: number; maxHeight: number } | null>(null)
  const triggerRef = useRef<HTMLButtonElement>(null)
  const menuRef = useRef<HTMLDivElement>(null)
  const searchRef = useRef<HTMLInputElement>(null)
  const typeaheadRef = useRef({ text: '', time: 0 })
  const active = options.find((option) => option.value === value)
  const visibleOptions = options.filter((option) => option.value === value || option.alwaysVisible
    || option.label.toLocaleLowerCase().includes(query.toLocaleLowerCase()))
  const groups = [...new Set(visibleOptions.map((option) => option.group ?? ''))]

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
      const spaceBelow = window.innerHeight - rect.bottom - MENU_MARGIN
      const spaceAbove = rect.top - MENU_MARGIN
      const menuHeight = menuRef.current?.offsetHeight ?? 0
      const dropUp = menuHeight > spaceBelow && spaceAbove > spaceBelow
      const maxHeight = Math.max(0, dropUp ? spaceAbove : spaceBelow)
      const top = dropUp
        ? Math.max(MENU_MARGIN, rect.top - Math.min(menuHeight, maxHeight) - MENU_MARGIN)
        : rect.bottom + MENU_MARGIN
      const minWidth = Math.min(window.innerWidth - MENU_MARGIN * 2, Math.max(rect.width, searchable ? 250 : 0))
      const width = Math.min(window.innerWidth - MENU_MARGIN * 2, Math.max(menuRef.current?.offsetWidth ?? 0, minWidth))
      const desiredLeft = align === 'right' ? rect.right - width : rect.left
      const left = Math.max(MENU_MARGIN, Math.min(desiredLeft, window.innerWidth - width - MENU_MARGIN))
      setMenuStyle({ top, left, minWidth, maxHeight })
    }

    place()
    window.addEventListener('scroll', place, true)
    window.addEventListener('resize', place)
    return () => {
      window.removeEventListener('scroll', place, true)
      window.removeEventListener('resize', place)
    }
  }, [open, align, options.length, query, searchable])

  /** Close the popup and optionally return keyboard focus to its trigger. */
  const close = ({ restoreFocus }: { restoreFocus: boolean }) => {
    setOpen(false)
    setQuery('')
    if (restoreFocus) triggerRef.current?.focus()
  }

  useEffect(() => {
    if (!open) return
    const handlePointerDown = (event: MouseEvent) => {
      const target = event.target as Node
      if (triggerRef.current?.contains(target)) return
      if (menuRef.current?.contains(target)) return
      close({ restoreFocus: false })
    }
    const handleKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        // Close only the menu, not a dialog that contains the trigger.
        event.stopPropagation()
        event.preventDefault()
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

  /** Only enabled choices participate in arrow-key navigation. */
  const optionButtons = () => Array.from(menuRef.current?.querySelectorAll<HTMLButtonElement>('[role="option"]:not(:disabled)') ?? [])

  // Once placed (a hidden menu cannot take focus), focus the selected option, or the requested end.
  const pendingFocus = useRef<'selected' | 'first' | 'last' | 'search'>('selected')
  const placed = menuStyle !== null
  useEffect(() => {
    if (!open || !placed) return
    if (pendingFocus.current === 'search' && searchRef.current) {
      searchRef.current.focus()
      return
    }
    const buttons = optionButtons()
    const selectedIndex = buttons.findIndex((button) => button.dataset.value === value)
    const index = pendingFocus.current === 'last'
      ? buttons.length - 1
      : pendingFocus.current === 'first' || selectedIndex < 0 ? 0 : selectedIndex
    if (buttons[index]) buttons[index].focus()
    else menuRef.current?.focus()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, placed])

  useEffect(() => {
    if (!open) setMenuStyle(null)
  }, [open])

  /** Open at the selected option or the requested end of the list. */
  const openWith = (focus: 'selected' | 'first' | 'last') => {
    pendingFocus.current = focus
    setOpen(true)
  }

  /** Let arrow keys open the menu without activating the enclosing form. */
  const handleTriggerKeyDown = (event: ReactKeyboardEvent<HTMLButtonElement>) => {
    if (open) return
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault()
      openWith(event.key === 'ArrowUp' && !active ? 'last' : 'selected')
    }
  }

  /** Support navigation, search, typeahead, and focus return from the popup. */
  const handleMenuKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    if (event.nativeEvent.isComposing) return
    const buttons = optionButtons()
    if (event.key === 'Tab') {
      close({ restoreFocus: true })
      return
    }
    const inSearch = event.target === searchRef.current
    if (inSearch && event.key === 'Enter') {
      event.preventDefault()
      const match = visibleOptions.find((option) => !option.disabled
        && option.label.toLocaleLowerCase().includes(query.toLocaleLowerCase()))
      if (match) {
        onChange(match.value)
        close({ restoreFocus: true })
      }
      return
    }
    if (buttons.length === 0) return
    const current = buttons.indexOf(document.activeElement as HTMLButtonElement)
    const move = (index: number) => {
      event.preventDefault()
      buttons[(index + buttons.length) % buttons.length].focus()
    }
    if (event.key === 'ArrowDown') move(current + 1)
    else if (event.key === 'ArrowUp') move(current < 0 ? buttons.length - 1 : current - 1)
    else if (!inSearch && event.key === 'Home') move(0)
    else if (!inSearch && event.key === 'End') move(buttons.length - 1)
    else if (!inSearch && event.key.length === 1 && event.key !== ' ' && !event.ctrlKey && !event.altKey && !event.metaKey) {
      event.preventDefault()
      if (searchable) {
        setQuery((currentQuery) => currentQuery + event.key)
        searchRef.current?.focus()
      } else {
        const now = Date.now()
        const text = (now - typeaheadRef.current.time < 700 ? typeaheadRef.current.text : '') + event.key.toLocaleLowerCase()
        typeaheadRef.current = { text, time: now }
        const prefix = [...text].every((letter) => letter === text[0]) ? text[0] : text
        const ordered = [...buttons.slice(current + 1), ...buttons.slice(0, current + 1)]
        ordered.find((button) => button.textContent?.trim().toLocaleLowerCase().startsWith(prefix))?.focus()
      }
    }
  }

  const menu = open && (
    <div
      ref={menuRef}
      tabIndex={-1}
      className="st-menu"
      onKeyDown={handleMenuKeyDown}
      style={{
        position: 'fixed',
        top: menuStyle?.top ?? -9999,
        left: menuStyle?.left ?? -9999,
        minWidth: menuStyle?.minWidth,
        maxWidth: 'calc(100vw - 8px)',
        maxHeight: menuStyle ? `min(17rem, ${menuStyle.maxHeight}px)` : undefined,
        visibility: menuStyle ? 'visible' : 'hidden',
      }}
    >
      {searchable && (
        <search className="st-menu-search">
          <label className="sr-only" htmlFor={`${menuId}-search`}>{searchLabel}</label>
          <input
            ref={searchRef}
            id={`${menuId}-search`}
            type="search"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder={searchLabel}
            className="st-input"
            autoCapitalize="none"
            autoCorrect="off"
            spellCheck={false}
          />
        </search>
      )}
      <div id={menuId} role="listbox" aria-label={ariaLabel}>
        {groups.map((group) => (
          <div key={group} role={group ? 'group' : undefined} aria-label={group || undefined}>
            {group && <div className="st-menu-group" aria-hidden="true">{group}</div>}
            {visibleOptions.filter((option) => (option.group ?? '') === group).map((option) => {
              const selected = option.value === value
              return (
                <button
                  key={option.value}
                  type="button"
                  role="option"
                  aria-selected={selected}
                  disabled={option.disabled}
                  data-value={option.value}
                  tabIndex={-1}
                  onClick={() => {
                    if (option.disabled) return
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
        ))}
      </div>
    </div>
  )

  return (
    <div className={`relative ${className}`}>
      <button
        ref={triggerRef}
        id={id}
        type="button"
        disabled={disabled}
        onClick={() => {
          pendingFocus.current = searchable ? 'search' : 'selected'
          if (open) close({ restoreFocus: false })
          else setOpen(true)
        }}
        onKeyDown={handleTriggerKeyDown}
        aria-label={ariaLabel}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={open ? menuId : undefined}
        aria-describedby={ariaDescribedBy}
        title={title ?? (active?.shortLabel ? active.label : undefined)}
        className={`st-select ${size === 'sm' ? 'st-select-sm' : ''}`}
      >
        <span className="flex min-w-0 items-center gap-1.5">
          {icon}
          <span className="st-select-value truncate">{active?.shortLabel ?? active?.label ?? placeholder ?? 'Select'}</span>
        </span>
        <ChevronDown size={12} className={`st-select-chevron shrink-0 transition-transform ${open ? 'rotate-180' : ''}`} />
      </button>
      {menu ? createPortal(menu, portalContainer()) : null}
    </div>
  )
}
