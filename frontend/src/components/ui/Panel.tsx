import type { ReactNode } from 'react'
import type { LucideIcon } from 'lucide-react'

/**
 * Building blocks for configuration surfaces — the settings modal and the
 * scheduled-task dialog.
 *
 * The shape every control follows is: a readable label, one sentence of plain
 * English underneath it, and the control itself on the right. Descriptions are
 * not optional decoration — they are the reason these panels are usable.
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
  hint,
  selected,
  tone = 'neutral',
  ariaLabel,
  onSelect,
}: {
  title: string
  summary: string
  hint?: string
  selected: boolean
  tone?: 'neutral' | 'ok' | 'warn'
  ariaLabel: string
  onSelect: () => void
}) {
  return (
    <button
      type="button"
      aria-label={ariaLabel}
      aria-pressed={selected}
      onClick={onSelect}
      className="st-choice"
    >
      <span className="flex items-center justify-between gap-2">
        <span className="st-label">{title}</span>
        {hint && <Badge tone={tone === 'warn' ? 'warn' : tone === 'ok' ? 'ok' : 'neutral'}>{hint}</Badge>}
      </span>
      <span className="st-desc mt-1.5 block">{summary}</span>
    </button>
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
  description: string
  action?: ReactNode
}) {
  return (
    <div className="mb-5 flex items-start justify-between gap-4">
      <div className="min-w-0">
        <h3 className="st-page-title">{title}</h3>
        <p className="st-page-desc mt-1.5">{description}</p>
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
