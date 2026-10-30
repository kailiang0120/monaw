import { useMemo, useState } from 'react'

import { updateSettings } from '../../lib/api/settings'
import type { AgentSettings } from '../../lib/api/types'
import type { StoredCredentialStatus } from '../../lib/apiKeySync'
import type { ConnectionSecretId, ConnectionSecretValues } from './connectionPortals/connectionPortalConfig'
import { normalizeDraft, type ModelOptionsCatalog } from './settingsConfig'
import { FOOTER_COPY } from './settingsCopy'

export type SaveOutcome = 'saved' | 'keys_failed' | 'failed'

const EMPTY_SECRETS: ConnectionSecretValues = { openai: '', google: '', tavily: '', telegramBot: '' }

/** Outside Electron, edited keys travel with the settings request instead of the OS store. */
const BROWSER_SECRET_FIELDS = {
  openai: 'openai_api_key',
  google: 'google_api_key',
  tavily: 'tavily_api_key',
  telegramBot: 'telegram_bot_token',
} as const satisfies Record<ConnectionSecretId, string>

/** The slice of settings that Save actually sends, used to detect edits. */
export function draftSignature(settings: AgentSettings): string {
  return JSON.stringify({
    llm: settings.llm,
    speech_to_text: settings.speech_to_text,
    mcp: settings.mcp,
    browser: settings.browser,
    memory: settings.memory,
    tools: settings.tools,
    permissions: settings.permissions,
    sandbox: settings.sandbox,
    identity: settings.identity,
    telegramUserIds: (settings.telegram_allowed_user_ids ?? '').trim(),
    telegramChatIds: (settings.telegram_allowed_chat_ids ?? '').trim(),
  })
}

function withCredentialStatus(
  settings: AgentSettings,
  status: StoredCredentialStatus,
  { merge }: { merge: boolean },
): AgentSettings {
  const keys = settings.api_keys
  return {
    ...settings,
    api_keys: {
      ...keys,
      has_openai_key: (merge && keys.has_openai_key) || status.openai,
      has_google_key: (merge && keys.has_google_key) || status.google,
      has_tavily_key: (merge && keys.has_tavily_key) || status.tavily,
      has_telegram_bot_token: (merge && Boolean(keys.has_telegram_bot_token)) || status.telegramBot,
    },
  }
}

function errorText(error: unknown): string {
  return error instanceof Error && error.message ? error.message : 'Unknown error.'
}

/**
 * Own the editable settings draft and the last saved copy it is compared with.
 *
 * Connection keys are drafts too: they are written when settings are saved.
 * MCP credential operations are immediate, so they reconcile only their own
 * metadata into both copies and leave unrelated edits in the draft.
 */
export function useSettingsDraft() {
  const [draft, setDraft] = useState<AgentSettings | null>(null)
  const [saved, setSaved] = useState<AgentSettings | null>(null)
  const [secrets, setSecrets] = useState<ConnectionSecretValues>(EMPTY_SECRETS)
  const [dirtySecrets, setDirtySecrets] = useState<Set<ConnectionSecretId>>(() => new Set())
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState('')

  const savedSignature = useMemo(() => (saved ? draftSignature(saved) : ''), [saved])

  const isDirty = useMemo(() => {
    if (!draft || !savedSignature) return false
    if (dirtySecrets.size > 0) return true
    return draftSignature(draft) !== savedSignature
  }, [draft, dirtySecrets, savedSignature])

  const load = (settings: AgentSettings) => {
    setDraft(settings)
    setSaved(settings)
  }

  const updateDraft = (updater: (current: AgentSettings) => AgentSettings) => {
    setDraft((current) => (current ? updater(current) : current))
  }

  const setSecret = (secret: ConnectionSecretId, value: string) => {
    setDirtySecrets((current) => new Set(current).add(secret))
    setSecrets((current) => ({ ...current, [secret]: value }))
  }

  /** Reflect keys already in the OS store, e.g. after syncing them to the backend on open. */
  const applyStoredCredentialStatus = (status: StoredCredentialStatus) => {
    setDirtySecrets(new Set())
    setDraft((current) => (current ? withCredentialStatus(current, status, { merge: true }) : current))
  }

  /** Reconcile an immediate MCP credential change without discarding other edits. */
  const reconcileMcpCredentials = (fresh: AgentSettings) => {
    setDraft((current) => current ? {
      ...current,
      settings_version: fresh.settings_version,
      mcp: {
        ...current.mcp,
        servers: current.mcp.servers.map((server) => {
          const savedServer = fresh.mcp.servers.find((item) => item.server_id === server.server_id)
          if (!savedServer) return server
          const ordinary = (target: 'env' | 'headers') => Object.fromEntries(Object.entries(server[target])
            .filter(([key]) => !savedServer.credential_refs?.[target]?.[key]))
          return {
            ...server,
            credential_refs: savedServer.credential_refs,
            legacy_secret_fields: savedServer.legacy_secret_fields,
            env: ordinary('env'),
            headers: ordinary('headers'),
          }
        }),
      },
    } : current)
    setSaved((current) => current ? { ...current, settings_version: fresh.settings_version, mcp: fresh.mcp } : current)
  }

  const writeElectronSecrets = async (): Promise<StoredCredentialStatus | null> => {
    if (!window.electronAPI) return null
    for (const secret of dirtySecrets) {
      const value = secrets[secret]
      if (value) await window.electronAPI.setCredential(secret, value)
      else await window.electronAPI.deleteCredential(secret)
    }
    return window.electronAPI.applyStoredCredentials()
  }

  /**
   * Save the draft, then any edited Electron keys. Saved settings become the
   * new baseline as soon as the backend accepts them, so a later key failure
   * leaves only the keys unsaved instead of a stale settings version.
   */
  const save = async (modelOptions: ModelOptionsCatalog): Promise<SaveOutcome> => {
    if (!draft) return 'failed'
    setSaving(true)
    setSaveError('')
    try {
      const browserSecretPayload: Partial<Record<(typeof BROWSER_SECRET_FIELDS)[ConnectionSecretId], string>> = {}
      if (!window.electronAPI) {
        for (const secret of dirtySecrets) browserSecretPayload[BROWSER_SECRET_FIELDS[secret]] = secrets[secret]
      }

      let savedSettings: AgentSettings
      try {
        savedSettings = normalizeDraft(await updateSettings({
          ...(draft.settings_version ? { expected_settings_version: draft.settings_version } : {}),
          llm: draft.llm,
          speech_to_text: draft.speech_to_text,
          mcp: draft.mcp,
          browser: draft.browser,
          memory: draft.memory,
          tools: draft.tools,
          permissions: draft.permissions,
          sandbox: draft.sandbox,
          identity: draft.identity,
          telegram_allowed_user_ids: draft.telegram_allowed_user_ids.trim(),
          telegram_allowed_chat_ids: draft.telegram_allowed_chat_ids.trim(),
          ...browserSecretPayload,
        }), modelOptions)
      } catch (error) {
        setSaveError(`${FOOTER_COPY.saveFailed} ${errorText(error)}`)
        return 'failed'
      }
      setDraft(savedSettings)
      setSaved(savedSettings)
      if (!window.electronAPI) {
        setDirtySecrets(new Set())
        return 'saved'
      }

      try {
        const credentialStatus = await writeElectronSecrets()
        if (credentialStatus) {
          setDraft((current) => (current ? withCredentialStatus(current, credentialStatus, { merge: false }) : current))
        }
        setDirtySecrets(new Set())
        return 'saved'
      } catch (error) {
        setSaveError(`${FOOTER_COPY.keysFailed} ${errorText(error)}`)
        return 'keys_failed'
      }
    } finally {
      setSaving(false)
    }
  }

  return {
    draft,
    isDirty,
    secrets,
    saving,
    saveError,
    load,
    updateDraft,
    setSecret,
    applyStoredCredentialStatus,
    reconcileMcpCredentials,
    save,
  }
}
