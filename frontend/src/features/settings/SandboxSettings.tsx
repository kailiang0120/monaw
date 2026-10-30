import { useState } from 'react'
import { AlertCircle } from 'lucide-react'

import { Dropdown } from '../../components/Dropdown'
import { ExecutionEnvironment } from '../../components/ExecutionEnvironment'
import { Badge, Note, NumberRow, PageHeader, SettingRow, SettingsCard, SwitchRow } from '../../components/ui/Panel'
import { resolveSandboxDockerImage } from '../../lib/api/settings'
import type { AgentSettings, SandboxStatus } from '../../lib/api/types'
import { PAGE_COPY, SANDBOX_COPY } from './settingsCopy'

type Sandbox = AgentSettings['sandbox']

interface Props {
  /** The draft's sandbox settings. */
  sandbox: Sandbox
  /** Status under the saved settings; unsaved edits are not reflected. */
  status: SandboxStatus | null
  onChange: (updater: (sandbox: Sandbox) => Sandbox) => void
  onStatus: (status: SandboxStatus) => void
}

export function SandboxSettings({ sandbox, status, onChange, onStatus }: Props) {
  const [resolvingDockerImage, setResolvingDockerImage] = useState(false)
  const [dockerResolveMessage, setDockerResolveMessage] = useState('')

  const resolveDockerImage = async () => {
    setResolvingDockerImage(true)
    setDockerResolveMessage('')
    try {
      const result = await resolveSandboxDockerImage(sandbox.docker.image)
      onChange((current) => ({ ...current, docker: { ...current.docker, image: result.image } }))
      setDockerResolveMessage(result.detail)
      onStatus(result.sandbox)
    } catch (error) {
      setDockerResolveMessage(error instanceof Error ? error.message : 'Docker image could not be resolved.')
    } finally {
      setResolvingDockerImage(false)
    }
  }

  return (
    <>
      <PageHeader title={PAGE_COPY.sandbox.label} description={PAGE_COPY.sandbox.blurb} />
      <div className="space-y-4">

        <SettingsCard>
          <SwitchRow
            label="Allow shell commands"
            checked={sandbox.enabled}
            onChange={(checked) => onChange((current) => ({ ...current, enabled: checked }))}
          />
        </SettingsCard>

        <SettingsCard title="Policy">
          <SettingRow
            label="Isolation mode"
            description={SANDBOX_COPY.modeHelp[sandbox.mode as keyof typeof SANDBOX_COPY.modeHelp] ?? SANDBOX_COPY.mode}
          >
            <Dropdown
              ariaLabel="Isolation mode"
              value={sandbox.mode}
              options={[
                { value: 'off', label: 'Off — no shell' },
                { value: 'auto', label: 'Automatic' },
                { value: 'enforce', label: 'Strong isolation only' },
                { value: 'host', label: 'Host, with approval' },
                { value: 'docker', label: 'Docker' },
                { value: 'local_restricted', label: 'Host, best effort' },
              ]}
              onChange={(mode) => onChange((current) => ({ ...current, mode }))}
            />
          </SettingRow>
          <SettingRow
            label="Container network"
            description={SANDBOX_COPY.networkHelp[sandbox.network.default as keyof typeof SANDBOX_COPY.networkHelp] ?? SANDBOX_COPY.network}
          >
            <Dropdown
              ariaLabel="Container network"
              value={sandbox.network.default}
              options={[
                { value: 'deny', label: 'Blocked' },
                { value: 'allow_with_approval', label: 'Follow approval mode' },
                { value: 'allow', label: 'Allowed' },
              ]}
              onChange={(defaultNetwork) => onChange((current) => ({
                ...current,
                network: { ...current.network, default: defaultNetwork },
              }))}
            />
          </SettingRow>
          <SettingRow
            label="Container files"
            description={SANDBOX_COPY.writeStrategyHelp[sandbox.default_write_strategy as keyof typeof SANDBOX_COPY.writeStrategyHelp] ?? SANDBOX_COPY.writeStrategy}
          >
            <Dropdown
              ariaLabel="Container files"
              value={sandbox.default_write_strategy}
              options={[
                { value: 'discard', label: 'Discard them' },
                { value: 'copy_out', label: 'Copy them back' },
                { value: 'direct_rw', label: 'Write directly' },
              ]}
              onChange={(strategy) => onChange((current) => ({ ...current, default_write_strategy: strategy }))}
            />
          </SettingRow>
        </SettingsCard>

        {sandbox.mode === 'enforce' && !status?.backends?.docker?.available && (
          <Note tone="warn" icon={AlertCircle}>{SANDBOX_COPY.enforceNeedsDocker}</Note>
        )}

        <SettingsCard title="Current status" description="Representative command under saved settings. Each actual command is classified again; unsaved changes are not applied.">
          <ExecutionEnvironment environment={status?.execution_environment} />
          <div className="st-row">
            <div className="min-w-0">
              <p className="st-label">Representative backend</p>
              <p className="st-desc mt-1">
                {status?.selected_backend || 'Checking…'} · {status?.isolation || 'unknown'} isolation
                {status?.reason_code ? ` · ${status.reason_code}` : ''}
              </p>
              {status?.reason && (
                <p className="st-desc mt-1">{status.reason}</p>
              )}
              {status?.fallback_backend && status.fallback_backend !== status.selected_backend && (
                <p className="st-desc mt-1">
                  PowerShell fallback: {status.fallback_backend} · {status.fallback_isolation}
                  {status.fallback_reason_code ? ` · ${status.fallback_reason_code}` : ''}
                </p>
              )}
              {status?.python_import_support && status.python_import_support !== 'probed' && (
                <p className="st-desc mt-1">
                  Python imports: {status.python_import_support}
                  {status.python_import_detail ? ` · ${status.python_import_detail}` : ''}
                </p>
              )}
            </div>
            <div className="st-row-control-auto">
              <Badge tone={status?.isolation === 'strong' ? 'ok' : 'warn'}>
                {status?.isolation || 'Checking…'}
              </Badge>
            </div>
          </div>
        </SettingsCard>

        <SettingsCard title="Available sandbox engines">
          {['docker', 'local_restricted', 'host'].map((backend) => {
            const backendStatus = status?.backends?.[backend]
            return (
              <div key={backend} className="st-row">
                <div className="min-w-0">
                  <p className="st-label capitalize">{backend.replace('_', ' ')}</p>
                  <p className="st-desc mt-1">
                    {backendStatus?.available ? `${backendStatus.security_label} isolation` : backendStatus?.reason || 'Checking…'}
                  </p>
                </div>
                <div className="st-row-control-auto">
                  <Badge tone={backendStatus?.available ? 'ok' : 'warn'}>
                    {backendStatus?.available ? 'Available' : 'Not available'}
                  </Badge>
                </div>
              </div>
            )
          })}
        </SettingsCard>

        <SettingsCard title="Docker">
          <SwitchRow
            label="Use Docker when available"
            checked={sandbox.docker.enabled}
            onChange={(checked) => onChange((current) => ({ ...current, docker: { ...current.docker, enabled: checked } }))}
          />
          <SettingRow label="Container image" description={SANDBOX_COPY.dockerImage} wide>
            <div className="flex w-full gap-2">
              <input
                value={sandbox.docker.image}
                aria-label="Container image"
                onChange={(e) => {
                  const image = e.target.value
                  onChange((current) => ({ ...current, docker: { ...current.docker, image } }))
                }}
                placeholder="python:3.12-slim@sha256:…"
                className="st-input min-w-0 flex-1 font-mono text-xs"
              />
              <button
                type="button"
                className="st-btn st-btn-secondary"
                disabled={resolvingDockerImage}
                onClick={() => void resolveDockerImage()}
              >
                {resolvingDockerImage ? 'Resolving…' : 'Resolve & pull'}
              </button>
            </div>
            {dockerResolveMessage && <p className="st-desc mt-2">{dockerResolveMessage}</p>}
          </SettingRow>
          <SwitchRow
            label="Read-only container"
            description={SANDBOX_COPY.dockerReadOnly}
            checked={sandbox.docker.read_only_root}
            onChange={(checked) => onChange((current) => ({ ...current, docker: { ...current.docker, read_only_root: checked } }))}
          />
        </SettingsCard>

        <SettingsCard title="Per-command limits">
          {([
            { key: 'timeout_seconds' as const, label: 'Time limit', unit: 'seconds' },
            { key: 'memory_mb' as const, label: 'Memory limit', unit: 'MB' },
            { key: 'cpus' as const, label: 'CPU limit', unit: 'cores' },
            { key: 'pids' as const, label: 'Process limit', unit: 'processes' },
          ]).map(({ key, label, unit }) => (
            <NumberRow
              key={key}
              label={label}
              ariaLabel={`Sandbox ${label.toLowerCase()}`}
              unit={unit}
              value={sandbox.resources[key]}
              onChange={(value) => onChange((current) => ({
                ...current,
                resources: { ...current.resources, [key]: Number(value) },
              }))}
            />
          ))}
        </SettingsCard>
      </div>
    </>
  )
}
