import { useState } from 'react'
import { AlertCircle, Plus, Trash2 } from 'lucide-react'

import { Dropdown } from '../../components/Dropdown'
import {
  Note,
  NumberRow,
  ReadOnlyValue,
  SettingRow,
  SettingsCard,
  StackedRow,
  Switch,
  SwitchRow,
} from '../../components/ui/Panel'
import { reconnectMCPServer } from '../../lib/api/diagnostics'
import type { AgentSettings, MCPServerDiagnostics } from '../../lib/api/types'
import { McpCredentials } from './McpCredentials'
import { MCP_SERVER_TEMPLATES, duplicateMcpServerNames, type MCPServerTemplateKey } from './settingsConfig'
import { MCP_COPY } from './settingsCopy'

type Mcp = AgentSettings['mcp']
type McpServer = Mcp['servers'][number]

interface Props {
  /** The draft's MCP settings. */
  mcp: Mcp
  /** Live connection state keyed by saved server name. */
  diagnostics: Record<string, MCPServerDiagnostics>
  onChange: (updater: (mcp: Mcp) => Mcp) => void
  onDiagnostic: (name: string, diagnostic: MCPServerDiagnostics) => void
  /** Credential operations apply immediately, so they wait for a clean, idle draft. */
  credentialsDisabled: boolean
  onCredentialsChanged: () => Promise<void>
}

export function McpSettings({
  mcp,
  diagnostics,
  onChange,
  onDiagnostic,
  credentialsDisabled,
  onCredentialsChanged,
}: Props) {
  const [mcpTemplate, setMcpTemplate] = useState<MCPServerTemplateKey>('custom')
  const [openMcpAdvanced, setOpenMcpAdvanced] = useState<Record<number, boolean>>({})
  const [reconnectingServer, setReconnectingServer] = useState('')

  const mcpFeatureEnabled = !!mcp.enabled
  const mcpFeatureDiagnostics = Object.values(diagnostics)
  const mcpFeatureAvailable = mcpFeatureDiagnostics[0]?.feature_available ?? true
  const mcpFeatureUnavailableReason = mcpFeatureDiagnostics[0]?.feature_unavailable_reason ?? ''
  const duplicateMcpNames = duplicateMcpServerNames(mcp.servers)

  const updateServer = (index: number, updater: (server: McpServer) => McpServer) => {
    onChange((current) => ({
      ...current,
      servers: current.servers.map((server, serverIndex) => serverIndex === index ? updater(server) : server),
    }))
  }

  const handleReconnectServer = async (name: string) => {
    if (!name) return
    setReconnectingServer(name)
    try {
      onDiagnostic(name, await reconnectMCPServer(name))
    } finally {
      setReconnectingServer('')
    }
  }

  return (
    <div role="tabpanel" id="settings-tools-panel-mcp" aria-labelledby="settings-tools-tab-mcp" className="space-y-4" tabIndex={0}>
      <SettingsCard
        title="External connections"
        description={MCP_COPY.intro}
        action={
          <div className="flex flex-wrap items-center gap-2">
            <Dropdown<MCPServerTemplateKey>
              ariaLabel="Server template"
              value={mcpTemplate}
              options={(Object.entries(MCP_SERVER_TEMPLATES) as Array<[MCPServerTemplateKey, { label: string; build: () => unknown }]>).map(
                ([key, template]) => ({ value: key, label: template.label }),
              )}
              onChange={setMcpTemplate}
              size="sm"
              className="min-w-44"
            />
            <button
              type="button"
              onClick={() => onChange((current) => ({
                ...current,
                servers: [...current.servers, MCP_SERVER_TEMPLATES[mcpTemplate].build()],
              }))}
              className="st-btn st-btn-primary st-btn-sm"
            >
              <Plus size={13} />
              Add server
            </button>
          </div>
        }
      >
        <SwitchRow
          label="Enable MCP servers"
          checked={mcpFeatureEnabled}
          onChange={(checked) => onChange((current) => ({ ...current, enabled: checked }))}
        />
      </SettingsCard>

      {!mcpFeatureAvailable && (
        <Note tone="warn" icon={AlertCircle}>
          The MCP bridge is built in, but the backend Python environment is missing the mcp package
          {mcpFeatureUnavailableReason ? ` (${mcpFeatureUnavailableReason})` : ''}.
        </Note>
      )}
      {duplicateMcpNames.length > 0 && (
        <Note tone="danger" icon={AlertCircle}>
          MCP server names must be unique. Duplicated: {duplicateMcpNames.join(', ')}
        </Note>
      )}

      {mcp.servers.length === 0 ? (
        <SettingsCard>
          <div className="p-8 text-center">
            <p className="st-label">No external connections</p>
            <p className="st-desc mx-auto mt-1.5">{MCP_COPY.intro}</p>
          </div>
        </SettingsCard>
      ) : (
        mcp.servers.map((server, index) => {
          const diagnostic = server.name ? diagnostics[server.name] : undefined
          const statusText = !mcpFeatureEnabled
            ? 'MCP bridge is off'
            : diagnostic?.feature_available === false
              ? 'Backend dependency missing'
              : diagnostic
                ? diagnostic.connected
                  ? `Connected — ${diagnostic.tool_count} tools`
                  : diagnostic.state === 'unhealthy'
                    ? `Unhealthy — ${diagnostic.unhealthy_reason || diagnostic.last_error || 'liveness check failed'}`
                    : diagnostic.last_error || 'Not connected'
                : 'Save or reconnect to check'

          return (
            <McpServerCard
              key={`mcp-server-${index}`}
              server={server}
              index={index}
              diagnostic={diagnostic}
              statusText={statusText}
              bridgeEnabled={mcpFeatureEnabled && (diagnostic?.feature_available ?? mcpFeatureAvailable)}
              reconnectingServer={reconnectingServer}
              advancedOpen={!!openMcpAdvanced[index]}
              onToggleAdvanced={() => setOpenMcpAdvanced((current) => ({ ...current, [index]: !current[index] }))}
              onReconnect={() => handleReconnectServer(server.name)}
              onRemove={() => onChange((current) => ({
                ...current,
                servers: current.servers.filter((_, serverIndex) => serverIndex !== index),
              }))}
              onUpdate={(updater) => updateServer(index, updater)}
              credentialsDisabled={credentialsDisabled}
              onCredentialsChanged={onCredentialsChanged}
            />
          )
        })
      )}

      {mcp.servers.length > 0 && <Note tone="warn">{MCP_COPY.plaintextWarning}</Note>}
    </div>
  )
}

function McpServerCard({
  server,
  index,
  diagnostic,
  statusText,
  bridgeEnabled,
  reconnectingServer,
  advancedOpen,
  onToggleAdvanced,
  onReconnect,
  onRemove,
  onUpdate,
  credentialsDisabled,
  onCredentialsChanged,
}: {
  server: AgentSettings['mcp']['servers'][number]
  index: number
  diagnostic?: MCPServerDiagnostics
  statusText: string
  bridgeEnabled: boolean
  reconnectingServer: string
  advancedOpen: boolean
  onToggleAdvanced: () => void
  onReconnect: () => void
  onRemove: () => void
  onUpdate: (updater: (server: AgentSettings['mcp']['servers'][number]) => AgentSettings['mcp']['servers'][number]) => void
  credentialsDisabled: boolean
  onCredentialsChanged: () => Promise<void>
}) {
  const dotClass = diagnostic?.connected
    ? 'st-dot st-dot-ok'
    : diagnostic?.last_error || diagnostic?.unhealthy_reason || diagnostic?.state === 'unhealthy'
      ? 'st-dot st-dot-warn'
      : 'st-dot'
  const isHttp = server.transport === 'streamable_http'

  return (
    <section className="st-card">
      <header className="st-card-head flex items-center gap-2">
        <span className={dotClass} />
        <input
          value={server.name}
          aria-label={`Server name ${index + 1}`}
          onChange={(e) => onUpdate((current) => ({ ...current, name: e.target.value }))}
          placeholder="Server name"
          className="st-input min-w-0 flex-1 font-medium"
        />
        <Switch
          label={`Enable server ${index + 1}`}
          checked={server.enabled}
          onChange={(checked) => onUpdate((current) => ({ ...current, enabled: checked }))}
        />
        <button
          type="button"
          onClick={onRemove}
          className="st-btn st-btn-ghost st-btn-icon"
          aria-label={`Remove MCP server ${index + 1}`}
        >
          <Trash2 size={13} />
        </button>
      </header>

      <SettingRow
        label="Connection type"
      >
        <Dropdown<AgentSettings['mcp']['servers'][number]['transport']>
          ariaLabel={`Transport for server ${index + 1}`}
          value={server.transport}
          options={[
            { value: 'stdio', label: 'Run a local program' },
            { value: 'streamable_http', label: 'Connect to a URL' },
          ]}
          onChange={(transport) => onUpdate((current) => ({ ...current, transport }))}
        />
      </SettingRow>

      <StackedRow
        label={isHttp ? 'Server URL' : 'Command'}
      >
        <input
          value={isHttp ? server.url : server.command}
          aria-label={isHttp ? `Server URL ${index + 1}` : `Command ${index + 1}`}
          onChange={(e) => onUpdate((current) => isHttp
            ? { ...current, url: e.target.value }
            : { ...current, command: e.target.value })}
          placeholder={isHttp ? 'http://127.0.0.1:3000/mcp' : 'npx'}
          className="st-input font-mono text-xs"
        />
      </StackedRow>

      {!isHttp && (
        <div className="st-row-stacked">
          <EditableList
            title="Arguments"
            emptyText="No arguments. Most servers need at least one."
            addLabel="Add argument"
            values={server.args}
            placeholder={(itemIndex) => `Argument ${itemIndex + 1}`}
            onAdd={() => onUpdate((current) => ({ ...current, args: [...current.args, ''] }))}
            onChange={(itemIndex, value) => onUpdate((current) => ({
              ...current,
              args: current.args.map((item, currentIndex) => currentIndex === itemIndex ? value : item),
            }))}
            onRemove={(itemIndex) => onUpdate((current) => ({
              ...current,
              args: current.args.filter((_, currentIndex) => currentIndex !== itemIndex),
            }))}
          />
        </div>
      )}

      {!isHttp && (
      <SettingRow label="Working folder" wide>
          <input
            value={server.cwd}
            aria-label={`Working directory ${index + 1}`}
            onChange={(e) => onUpdate((current) => ({ ...current, cwd: e.target.value }))}
            placeholder="Optional"
            className="st-input font-mono text-xs"
          />
        </SettingRow>
      )}

      <SettingRow label="Personal note" wide>
        <input
          value={server.description}
          aria-label={`Server description ${index + 1}`}
          onChange={(e) => onUpdate((current) => ({ ...current, description: e.target.value }))}
          placeholder="Optional"
          className="st-input"
        />
      </SettingRow>

      <div className="st-card-foot flex flex-wrap items-center justify-between gap-3">
        <span
          className="st-hint"
          style={{
            color: diagnostic?.connected
              ? 'var(--st-ok)'
              : diagnostic?.last_error
                ? 'var(--st-warn)'
                : 'var(--st-text-faint)',
          }}
        >
          {statusText}
          {diagnostic?.state ? ` · ${diagnostic.state}` : ''}
        </span>
        <div className="flex items-center gap-2">
          <button type="button" onClick={onToggleAdvanced} className="st-btn st-btn-ghost">
            {advancedOpen ? 'Hide advanced' : 'Advanced'}
          </button>
          <button
            type="button"
            onClick={onReconnect}
            disabled={!server.name || reconnectingServer === server.name || !bridgeEnabled}
            className="st-btn st-btn-secondary"
          >
            {reconnectingServer === server.name ? 'Reconnecting…' : 'Reconnect'}
          </button>
        </div>
      </div>

      {advancedOpen && (
        <div className="st-divider-t">
          <McpCredentials key={`${server.server_id}-${server.transport}`} server={server}
            disabled={credentialsDisabled} onChanged={onCredentialsChanged} />
          <NumberRow
            label="Startup timeout"
            ariaLabel={`Startup timeout for server ${index + 1}`}
            unit="ms"
            min={1000}
            value={server.startup_timeout_ms}
            onChange={(value) => onUpdate((current) => ({ ...current, startup_timeout_ms: Number(value || 0) }))}
          />
          <NumberRow
            label="Call timeout"
            ariaLabel={`Call timeout for server ${index + 1}`}
            unit="ms"
            min={1000}
            value={server.call_timeout_ms}
            onChange={(value) => onUpdate((current) => ({ ...current, call_timeout_ms: Number(value || 0) }))}
          />
          <SwitchRow
            label="Reconnect automatically"
            checked={server.reconnect_on_unhealthy}
            onChange={(checked) => onUpdate((current) => ({ ...current, reconnect_on_unhealthy: checked }))}
          />
          <div className="st-row-stacked">
            <EditableList
              title="Tool allow list"
              emptyText={MCP_COPY.allowList}
              addLabel="Add tool"
              values={server.allow_list}
              placeholder={() => 'Tool name'}
              onAdd={() => onUpdate((current) => ({ ...current, allow_list: [...current.allow_list, ''] }))}
              onChange={(toolIndex, value) => onUpdate((current) => ({
                ...current,
                allow_list: current.allow_list.map((item, currentIndex) => currentIndex === toolIndex ? value : item),
              }))}
              onRemove={(toolIndex) => onUpdate((current) => ({
                ...current,
                allow_list: current.allow_list.filter((_, currentIndex) => currentIndex !== toolIndex),
              }))}
            />
          </div>
          <div className="st-row-stacked">
            <EditableList
              title="Trusted tools"
              emptyText={MCP_COPY.trustedTools}
              addLabel="Add trusted tool"
              values={server.trusted_tools}
              placeholder={() => 'Remote tool name'}
              onAdd={() => onUpdate((current) => ({ ...current, trusted_tools: [...current.trusted_tools, ''] }))}
              onChange={(toolIndex, value) => onUpdate((current) => ({
                ...current,
                trusted_tools: current.trusted_tools.map((item, currentIndex) => currentIndex === toolIndex ? value : item),
              }))}
              onRemove={(toolIndex) => onUpdate((current) => ({
                ...current,
                trusted_tools: current.trusted_tools.filter((_, currentIndex) => currentIndex !== toolIndex),
              }))}
            />
          </div>
          <div className="st-row-stacked">
            <KeyValueEditor
              title="Tool risk overrides"
              addLabel="Add override"
              emptyText={MCP_COPY.riskOverrides}
              entries={server.tool_risk_overrides as Record<string, string>}
              keyPlaceholder="Remote tool name"
              valuePlaceholder="low, medium, or high"
              onAdd={() => onUpdate((current) => ({
                ...current,
                tool_risk_overrides: { ...current.tool_risk_overrides, '': 'medium' },
              }))}
              onChange={(entryIndex, key, value) => onUpdate((current) => ({
                ...current,
                tool_risk_overrides: renameEntry(
                  current.tool_risk_overrides as Record<string, string>,
                  entryIndex,
                  key,
                  value,
                ) as AgentSettings['mcp']['servers'][number]['tool_risk_overrides'],
              }))}
              onRemove={(entryIndex) => onUpdate((current) => ({
                ...current,
                tool_risk_overrides: removeEntry(
                  current.tool_risk_overrides as Record<string, string>,
                  entryIndex,
                ) as AgentSettings['mcp']['servers'][number]['tool_risk_overrides'],
              }))}
            />
          </div>
          <div className="st-row-stacked">
            {isHttp ? (
              <KeyValueEditor
                title="HTTP headers"
                addLabel="Add header"
                emptyText="No headers configured."
                entries={server.headers}
                keyPlaceholder="Header"
                valuePlaceholder="Value"
                onAdd={() => onUpdate((current) => ({ ...current, headers: { ...current.headers, '': '' } }))}
                onChange={(entryIndex, key, value) => onUpdate((current) => ({
                  ...current,
                  headers: renameEntry(current.headers, entryIndex, key, value),
                }))}
                onRemove={(entryIndex) => onUpdate((current) => ({
                  ...current,
                  headers: removeEntry(current.headers, entryIndex),
                }))}
              />
            ) : (
              <KeyValueEditor
                title="Environment variables"
                addLabel="Add variable"
                emptyText="No environment variables configured."
                entries={server.env}
                keyPlaceholder="Name"
                valuePlaceholder="Value"
                onAdd={() => onUpdate((current) => ({ ...current, env: { ...current.env, '': '' } }))}
                onChange={(entryIndex, key, value) => onUpdate((current) => ({
                  ...current,
                  env: renameEntry(current.env, entryIndex, key, value),
                }))}
                onRemove={(entryIndex) => onUpdate((current) => ({
                  ...current,
                  env: removeEntry(current.env, entryIndex),
                }))}
              />
            )}
          </div>
          {diagnostic && (
            <div className="st-row-stacked">
              <McpDiagnostics diagnostic={diagnostic} />
            </div>
          )}
        </div>
      )}
    </section>
  )
}

/** Replace the entry at `index`, preserving key order. */
function renameEntry(
  entries: Record<string, string>,
  index: number,
  key: string,
  value: string,
): Record<string, string> {
  const next: Record<string, string> = {}
  Object.entries(entries).forEach(([entryKey, entryValue], currentIndex) => {
    if (currentIndex === index) next[key] = value
    else next[entryKey] = entryValue
  })
  return next
}

function removeEntry(entries: Record<string, string>, index: number): Record<string, string> {
  const next: Record<string, string> = {}
  Object.entries(entries).forEach(([entryKey, entryValue], currentIndex) => {
    if (currentIndex !== index) next[entryKey] = entryValue
  })
  return next
}

function EditableList({
  title,
  emptyText,
  addLabel,
  values,
  placeholder,
  onAdd,
  onChange,
  onRemove,
}: {
  title: string
  emptyText: string
  addLabel: string
  values: string[]
  placeholder: (index: number) => string
  onAdd: () => void
  onChange: (index: number, value: string) => void
  onRemove: (index: number) => void
}) {
  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between gap-3">
        <p className="st-label">{title}</p>
        <button type="button" onClick={onAdd} className="st-btn st-btn-ghost">
          <Plus size={12} />
          {addLabel}
        </button>
      </div>
      {values.length === 0 ? (
        <p className="st-hint">{emptyText}</p>
      ) : (
        values.map((value, itemIndex) => (
          <div key={`${title}-${itemIndex}`} className="flex gap-2">
            <input
              value={value}
              aria-label={`${title} ${itemIndex + 1}`}
              onChange={(e) => onChange(itemIndex, e.target.value)}
              placeholder={placeholder(itemIndex)}
              className="st-input min-w-0 flex-1 font-mono text-xs"
            />
            <button
              type="button"
              onClick={() => onRemove(itemIndex)}
              className="st-btn st-btn-ghost st-btn-icon shrink-0"
              aria-label={`Remove ${title} item ${itemIndex + 1}`}
            >
              <Trash2 size={13} />
            </button>
          </div>
        ))
      )}
    </div>
  )
}

function KeyValueEditor({
  title,
  addLabel,
  emptyText,
  entries,
  keyPlaceholder,
  valuePlaceholder,
  onAdd,
  onChange,
  onRemove,
}: {
  title: string
  addLabel: string
  emptyText: string
  entries: Record<string, string>
  keyPlaceholder: string
  valuePlaceholder: string
  onAdd: () => void
  onChange: (entryIndex: number, key: string, value: string) => void
  onRemove: (entryIndex: number) => void
}) {
  const entryList = Object.entries(entries)
  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between gap-3">
        <p className="st-label">{title}</p>
        <button type="button" onClick={onAdd} className="st-btn st-btn-ghost">
          <Plus size={12} />
          {addLabel}
        </button>
      </div>
      {entryList.length === 0 ? (
        <p className="st-hint">{emptyText}</p>
      ) : (
        entryList.map(([entryKey, entryValue], entryIndex) => (
          <div key={`${title}-${entryIndex}`} className="grid grid-cols-[1fr_1fr_auto] gap-2">
            <input
              value={entryKey}
              aria-label={`${title} name ${entryIndex + 1}`}
              onChange={(e) => onChange(entryIndex, e.target.value, entryValue)}
              placeholder={keyPlaceholder}
              className="st-input min-w-0 font-mono text-xs"
            />
            <input
              value={entryValue}
              aria-label={`${title} value ${entryIndex + 1}`}
              onChange={(e) => onChange(entryIndex, entryKey, e.target.value)}
              placeholder={valuePlaceholder}
              className="st-input min-w-0 font-mono text-xs"
            />
            <button
              type="button"
              onClick={() => onRemove(entryIndex)}
              className="st-btn st-btn-ghost st-btn-icon"
              aria-label={`Remove ${title} item ${entryIndex + 1}`}
            >
              <Trash2 size={13} />
            </button>
          </div>
        ))
      )}
    </div>
  )
}

function McpDiagnostics({ diagnostic }: { diagnostic: MCPServerDiagnostics }) {
  return (
    <div className="space-y-2">
      <p className="st-label">Diagnostics</p>
      <div className="grid gap-2 sm:grid-cols-2">
        <ReadOnlyValue
          label={diagnostic.transport === 'streamable_http' ? 'URL' : 'Command'}
          value={diagnostic.transport === 'streamable_http'
            ? diagnostic.url || ''
            : [diagnostic.command, ...(diagnostic.args || [])].filter(Boolean).join(' ')}
        />
        <ReadOnlyValue label="Working directory" value={diagnostic.cwd || ''} />
        <ReadOnlyValue label="Executable" value={diagnostic.resolved_executable || ''} />
        <ReadOnlyValue label="Process ID" value={diagnostic.pid ? String(diagnostic.pid) : ''} />
        <ReadOnlyValue label="Connected at" value={diagnostic.connected_at || ''} />
        <ReadOnlyValue
          label="Last call"
          value={diagnostic.last_call_duration_ms ? `${diagnostic.last_call_duration_ms} ms` : ''}
        />
        <ReadOnlyValue label="Failed calls" value={String(diagnostic.failed_call_count)} />
        <ReadOnlyValue label="Tools exposed" value={String(diagnostic.reflected_tool_names?.length ?? 0)} />
      </div>
      {diagnostic.unhealthy_reason && <Note tone="warn">{diagnostic.unhealthy_reason}</Note>}
      {(diagnostic.reflected_tool_names?.length ?? 0) > 0 && (
        <ReadOnlyValue label="Tool names" value={diagnostic.reflected_tool_names.join(', ')} />
      )}
      {diagnostic.stderr_tail && (
        <pre className="st-code max-h-32 overflow-auto whitespace-pre-wrap">{diagnostic.stderr_tail}</pre>
      )}
    </div>
  )
}
