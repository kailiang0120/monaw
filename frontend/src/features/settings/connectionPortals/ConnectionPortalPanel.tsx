import { Check, Eye, EyeOff, X } from 'lucide-react'

import { Dropdown } from '../../../components/Dropdown'
import {
  CONNECTION_PORTAL_OPTIONS,
  CONNECTION_PORTALS,
  CONNECTION_SECRET_FIELDS,
  type ConnectionPortalId,
  type ConnectionSecretId,
  type ConnectionSecretStatuses,
  type ConnectionSecretValues,
} from './connectionPortalConfig'

interface Props {
  selectedPortal: ConnectionPortalId
  values: ConnectionSecretValues
  statuses: ConnectionSecretStatuses
  showSecrets: boolean
  onPortalChange: (portal: ConnectionPortalId) => void
  onSecretChange: (secret: ConnectionSecretId, value: string) => void
  onToggleSecrets: () => void
}

export function ConnectionPortalPanel({
  selectedPortal,
  values,
  statuses,
  showSecrets,
  onPortalChange,
  onSecretChange,
  onToggleSecrets,
}: Props) {
  const portal = CONNECTION_PORTALS.find((item) => item.id === selectedPortal) ?? CONNECTION_PORTALS[0]

  return (
    <div className="space-y-4">
      <div className="space-y-1.5">
        <span className="section-label">Portal</span>
        <Dropdown<ConnectionPortalId>
          value={portal.id}
          options={CONNECTION_PORTAL_OPTIONS}
          onChange={onPortalChange}
          ariaLabel="Portal"
        />
      </div>

      <div className="rounded-lg border border-accent/20 bg-accent/10 px-3 py-2 text-xs leading-relaxed text-neutral-400">
        {portal.description}
      </div>

      {portal.secretIds.map((secret) => {
        const field = CONNECTION_SECRET_FIELDS[secret]
        return (
          <SecretField
            key={secret}
            label={field.label}
            statusLabel={field.statusLabel}
            placeholder={field.placeholder}
            value={values[secret]}
            saved={statuses[secret]}
            show={showSecrets}
            onChange={(value) => onSecretChange(secret, value)}
            onDelete={() => onSecretChange(secret, '')}
            onToggle={onToggleSecrets}
          />
        )
      })}

      <p className="text-xs leading-relaxed text-neutral-600">
        Saved credentials are encrypted by the operating system through Electron. Existing values are write-only and are never returned to this page.
      </p>
    </div>
  )
}

function SecretField({
  label,
  statusLabel,
  placeholder,
  value,
  saved,
  show,
  onChange,
  onDelete,
  onToggle,
}: {
  label: string
  statusLabel: string
  placeholder: string
  value: string
  saved: boolean
  show: boolean
  onChange: (value: string) => void
  onDelete: () => void
  onToggle: () => void
}) {
  return (
    <div className="space-y-1.5">
      <div className="flex items-center gap-1.5">
        <span className="section-label">{label}</span>
        {saved ? (
          <Check size={11} className="text-emerald-400" strokeWidth={3} />
        ) : (
          <X size={11} className="text-red-400" strokeWidth={3} />
        )}
        <span className="sr-only">{statusLabel}: {saved ? 'yes' : 'no'}</span>
      </div>
      <div className="relative">
        <input
          type={show ? 'text' : 'password'}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder={placeholder}
          className="control w-full rounded-xl px-3 py-2 pr-10 text-sm"
        />
        <button
          type="button"
          onClick={onToggle}
          className="ghost-button absolute right-2 top-1/2 h-7 w-7 -translate-y-1/2 rounded-lg"
          aria-label={show ? 'Hide secret' : 'Show secret'}
        >
          {show ? <EyeOff size={14} /> : <Eye size={14} />}
        </button>
      </div>
      {saved && !value && (
        <button
          type="button"
          onClick={onDelete}
          className="text-[11px] text-red-300 hover:text-red-200 hover:underline"
          aria-label={`Remove stored ${label}`}
        >
          Clear saved credential
        </button>
      )}
    </div>
  )
}
