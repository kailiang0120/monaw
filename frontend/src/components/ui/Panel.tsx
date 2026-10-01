import { useEffect, useId, useLayoutEffect, useRef, useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { Info, type LucideIcon } from 'lucide-react'

/**
 * Building blocks for configuration surfaces — the settings modal and the
 * scheduled-task dialog.
 *
 * Controls pair a short label with a brief summary. Longer explanations can
 * live behind an info button to keep the configuration surface compact.
 *
 * Styling lives in styles/settings.css and reads the theme tokens, so these
 * components carry no per-theme logic.
 */

/** A bordered group of related settings, with an optional title and blurb. */
export function SettingsCard({
  title,
  description,
  action,
  footnote,
  children,
}: {
  title?: string
  description?: string
  action?: ReactNode
  footnote?: ReactNode
  children: ReactNode
}) {
  return (
    <section className="st-card">
      {(title || action) && (
        <header className="st-card-head flex items-start justify-between gap-4">
          <div className="min-w-0">
            {title && <h4 className="st-label">{title}</h4>}
            {description && <p className="st-desc mt-1">{description}</p>}
          </div>
          {action && <div className="shrink-0">{action}</div>}
        </header>
      )}
      <div>{children}</div>
      {footnote && <div className="st-card-foot st-hint">{footnote}</div>}
    </section>
  )
}

/** Label + description on the left, control on the right. */
export function SettingRow({
  label,
  description,
  children,
  wide,
}: {
  label: string
  description?: string
  children: ReactNode
  /** Give the control more room, for wide inputs like paths. */
  wide?: boolean
}) {
  return (
    <div className="st-row">
      <div className="min-w-0">
        <p className="st-label">{label}</p>
        {description && <p className="st-desc mt-1">{description}</p>}
      </div>
      <div className={wide ? 'st-row-control-auto w-1/2 min-w-0' : 'st-row-control'}>{children}</div>
    </div>
  )
}

/** Label + description with the control on its own line underneath. */
export function StackedRow({
  label,
  description,
  action,
  children,
}: {
  label: string
  description?: string
  action?: ReactNode
  children: ReactNode
}) {
  return (
    <div className="st-row-stacked">
      <div className="mb-2 flex items-start justify-between gap-4">
        <div className="min-w-0">
          <p className="st-label">{label}</p>
          {description && <p className="st-desc mt-1">{description}</p>}
        </div>
        {action && <div className="shrink-0">{action}</div>}
      </div>
      {children}
    </div>
  )
}

/**
 * A real on/off switch.
 *
 * The input stays a native checkbox (visually hidden) so it keeps keyboard
 * behaviour and shows up in the accessibility tree; `aria-label` carries the
 * short name so the surrounding description never muddies the accessible name.
 */
export function Switch({
  label,
  checked,
  onChange,
  disabled,
}: {
  label: string
  checked: boolean
  onChange: (checked: boolean) => void
  disabled?: boolean
}) {
  return (
    <label className="inline-flex items-center">
      <input
        type="checkbox"
        className="st-switch-input sr-only"
        aria-label={label}
        checked={checked}
        disabled={disabled}
        onChange={(event) => onChange(event.target.checked)}
      />
      <span className="st-switch" aria-hidden="true" />
    </label>
  )
}

/** A full settings row whose control is a switch. */
export function SwitchRow({
  label,
  description,
  checked,
  onChange,
  disabled,
}: {
  label: string
  description?: string
  checked: boolean
  onChange: (checked: boolean) => void
  disabled?: boolean
}) {
  return (
    <div className="st-row">
      <div className="min-w-0">
        <p className="st-label">{label}</p>
        {description && <p className="st-desc mt-1">{description}</p>}
      </div>
      <div className="st-row-control-auto pt-0.5">
        <Switch label={label} checked={checked} onChange={onChange} disabled={disabled} />
      </div>
    </div>
  )
}

/** A number input with a trailing unit, used for limits and quotas. */
export function NumberRow({
  label,
  description,
  ariaLabel,
  unit,
  value,
  min,
  max,
  onChange,
}: {
  label: string
  description?: string
  ariaLabel: string
  unit?: string
  value: number
  min?: number
  max?: number
  onChange: (value: string) => void
}) {
  return (
    <div className="st-row">
      <div className="min-w-0">
        <p className="st-label">{label}</p>
        {description && <p className="st-desc mt-1">{description}</p>}
      </div>
      <div className="st-row-control-auto flex items-center gap-2">
        <input
          type="number"
          aria-label={ariaLabel}
          min={min}
          max={max}
          value={value}
          onChange={(event) => onChange(event.target.value)}
          className="st-input st-input-number"
        />
        {unit && <span className="st-hint w-10 shrink-0">{unit}</span>}
      </div>
    </div>
  )
}

/** A short highlighted note. Use sparingly — one per card at most. */
export function Note({
  tone = 'info',
  icon: Icon,
  children,
}: {
  tone?: 'neutral' | 'info' | 'warn' | 'danger'
  icon?: LucideIcon
  children: ReactNode
}) {
  const toneClass = {
    neutral: 'st-note',
    info: 'st-note st-note-info',
    warn: 'st-note st-note-warn',
    danger: 'st-note st-note-danger',
  }[tone]

  return (
    <div className={`${toneClass} flex gap-2`}>
      {Icon && <Icon size={14} className="mt-0.5 shrink-0" />}
      <div className="min-w-0">{children}</div>
    </div>
  )
}

/** A small status chip. */
export function Badge({
  tone = 'neutral',
  children,
}: {
  tone?: 'neutral' | 'ok' | 'warn' | 'danger' | 'accent'
  children: ReactNode
}) {
  const toneClass = {
    neutral: 'st-badge',
    ok: 'st-badge st-badge-ok',
    warn: 'st-badge st-badge-warn',
    danger: 'st-badge st-badge-danger',
    accent: 'st-badge st-badge-accent',
  }[tone]

  return <span className={toneClass}>{children}</span>
}

/** A selectable card used where the choice needs explaining, not just naming. */
export function ChoiceCard({
  title,
  summary,
  description,
  hint,
  selected,
  tone = 'neutral',
  ariaLabel,
  onSelect,
}: {
  title: string
  summary: string
  description?: string
  hint?: string
  selected: boolean
  tone?: 'neutral' | 'ok' | 'warn' | 'danger'
  ariaLabel: string
  onSelect: () => void
}) {
  return (
    <div className="relative h-full">
      <button
        type="button"
        aria-label={ariaLabel}
        aria-pressed={selected}
        data-tone={tone}
        onClick={onSelect}
        className={`st-choice h-full ${description ? 'pr-10' : ''}`}
      >
        <span className="st-label block">{title}</span>
        <span className="st-desc mt-1 block">{summary}</span>
        {hint && <span className="mt-2 block"><Badge tone={tone}>{hint}</Badge></span>}
      </button>
      {description && <InfoTip label={`About ${title}`} description={description} />}
    </div>
  )
}

/** A separate help button so reading a choice's details never selects it. */
function InfoTip({ label, description }: { label: string; description: string }) {
  const id = useId()
  const triggerRef = useRef<HTMLButtonElement>(null)
  const tooltipRef = useRef<HTMLDivElement>(null)
  const [open, setOpen] = useState(false)
  const [position, setPosition] = useState({ top: 0, left: 0, width: 280 })

  useLayoutEffect(() => {
    if (!open) return
    const place = () => {
      const rect = triggerRef.current?.getBoundingClientRect()
      if (!rect) return
      const width = Math.min(280, window.innerWidth - 24)
      const height = tooltipRef.current?.offsetHeight ?? 0
      setPosition({
        width,
        left: Math.max(12, Math.min(rect.right - width, window.innerWidth - width - 12)),
        top: rect.bottom + height + 8 <= window.innerHeight ? rect.bottom : Math.max(12, rect.top - height),
      })
    }
    place()
    window.addEventListener('resize', place)
    window.addEventListener('scroll', place, true)
    return () => {
      window.removeEventListener('resize', place)
      window.removeEventListener('scroll', place, true)
    }
  }, [open])

  useEffect(() => {
    if (!open) return
    const dismiss = (event: PointerEvent) => {
      if (!triggerRef.current?.contains(event.target as Node) && !tooltipRef.current?.contains(event.target as Node)) setOpen(false)
    }
    const escape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.stopPropagation()
        setOpen(false)
      }
    }
    window.addEventListener('pointerdown', dismiss)
    window.addEventListener('keydown', escape, true)
    return () => {
      window.removeEventListener('pointerdown', dismiss)
      window.removeEventListener('keydown', escape, true)
    }
  }, [open])

  const container = triggerRef.current?.closest('.theme-dark, .theme-light') ?? document.body
  return (
    <span
      className="absolute right-2 top-2"
      onMouseEnter={() => setOpen(true)}
      onMouseLeave={() => setOpen(false)}
    >
      <button
        ref={triggerRef}
        type="button"
        aria-label={label}
        aria-describedby={open ? id : undefined}
        onFocus={() => setOpen(true)}
        onBlur={() => setOpen(false)}
        onClick={() => setOpen(true)}
        className="st-info-button"
      >
        <Info size={14} aria-hidden="true" />
      </button>
      {open && createPortal(
        <div ref={tooltipRef} id={id} role="tooltip" className="st-info-tip" style={position}>
          {description}
        </div>,
        container,
      )}
    </span>
  )
}

/** Read-only key/value line, for paths and other computed values. */
export function ReadOnlyValue({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <p className="st-hint mb-1">{label}</p>
      <p className="st-code">{value || '—'}</p>
    </div>
  )
}

/** Header at the top of each settings page. */
export function PageHeader({
  title,
  description,
  action,
}: {
  title: string
  description?: string
  action?: ReactNode
}) {
  return (
    <div className="mb-5 flex items-start justify-between gap-4">
      <div className="min-w-0">
        <h3 className="st-page-title">{title}</h3>
        {description && <p className="st-page-desc mt-1.5">{description}</p>}
      </div>
      {action && <div className="shrink-0">{action}</div>}
    </div>
  )
}

/** A compact label + switch pair, for grids of related flags. */
export function FlagToggle({
  label,
  ariaLabel,
  checked,
  onChange,
}: {
  label: string
  ariaLabel: string
  checked: boolean
  onChange: (checked: boolean) => void
}) {
  return (
    <div className="flex items-center justify-between gap-3">
      <span className="st-desc">{label}</span>
      <Switch label={ariaLabel} checked={checked} onChange={onChange} />
    </div>
  )
}
