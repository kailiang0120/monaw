import { useEffect, useRef, useState } from 'react'
import { Check, ChevronUp } from 'lucide-react'

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
  const wrapperRef = useRef<HTMLDivElement>(null)
  const active = options.find((option) => option.value === value)

  useEffect(() => {
    if (!open) return
    const handlePointerDown = (event: MouseEvent) => {
      if (wrapperRef.current && !wrapperRef.current.contains(event.target as Node)) {
        setOpen(false)
      }
    }
    const handleKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false)
    }
    window.addEventListener('mousedown', handlePointerDown)
    window.addEventListener('keydown', handleKey)
    return () => {
      window.removeEventListener('mousedown', handlePointerDown)
      window.removeEventListener('keydown', handleKey)
    }
  }, [open])

  const sizeClass = size === 'sm' ? 'h-7 px-2.5 text-[11px]' : 'h-9 px-3 text-xs'

  return (
    <div ref={wrapperRef} className={`relative ${className}`}>
      <button
        type="button"
        disabled={disabled}
        onClick={() => setOpen((current) => !current)}
        aria-label={ariaLabel}
        aria-expanded={open}
        className={`inline-flex w-full items-center justify-between gap-2 rounded-lg border border-white/[0.08] bg-white/[0.03] font-medium text-neutral-300 outline-none transition-colors hover:text-neutral-100 focus:border-accent/60 disabled:cursor-not-allowed disabled:opacity-50 ${sizeClass}`}
      >
        <span className="truncate">{active?.label ?? placeholder ?? 'Select'}</span>
        <ChevronUp
          size={12}
          className={`shrink-0 transition-transform ${open ? '' : 'rotate-180'}`}
        />
      </button>
      {open && (
        <div
          className={`absolute z-30 mt-1 min-w-full overflow-hidden rounded-lg border border-white/[0.1] bg-[#171615] py-1 text-[11px] shadow-2xl shadow-black/40 ${
            align === 'right' ? 'right-0' : 'left-0'
          }`}
        >
          {options.map((option) => {
            const selected = option.value === value
            return (
              <button
                key={option.value}
                type="button"
                onClick={() => {
                  onChange(option.value)
                  setOpen(false)
                }}
                className={`flex w-full items-center justify-between gap-2 whitespace-nowrap px-3 py-2 text-left transition-colors ${
                  selected
                    ? 'bg-accent/20 text-neutral-100'
                    : 'text-neutral-400 hover:bg-white/[0.05] hover:text-neutral-100'
                }`}
              >
                <span>{option.label}</span>
                {selected && <Check size={12} className="text-accent-light" />}
              </button>
            )
          })}
        </div>
      )}
    </div>
  )
}
