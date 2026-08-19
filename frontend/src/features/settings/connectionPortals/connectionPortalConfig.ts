export type ConnectionPortalId = 'modelProviders' | 'webSearch' | 'telegram'

export type ConnectionSecretId =
  | 'openai'
  | 'google'
  | 'tavily'
  | 'telegramBot'

export type ConnectionSecretValues = Record<ConnectionSecretId, string>
export type ConnectionSecretStatuses = Record<ConnectionSecretId, boolean>

export const CONNECTION_SECRET_FIELDS: Record<
  ConnectionSecretId,
  {
    label: string
    statusLabel: string
    placeholder: string
  }
> = {
  openai: {
    label: 'OpenAI API Key',
    statusLabel: 'OpenAI key saved',
    placeholder: 'OpenAI API Key',
  },
  google: {
    label: 'Google API Key',
    statusLabel: 'Google key saved',
    placeholder: 'Google API Key',
  },
  tavily: {
    label: 'Tavily API Key',
    statusLabel: 'Tavily key saved',
    placeholder: 'Tavily API Key',
  },
  telegramBot: {
    label: 'Telegram Bot Token',
    statusLabel: 'Telegram token saved',
    placeholder: 'Telegram Bot Token',
  },
}

export const CONNECTION_PORTALS: Array<{
  id: ConnectionPortalId
  label: string
  description: string
  secretIds: ConnectionSecretId[]
}> = [
  {
    id: 'modelProviders',
    label: 'Model providers',
    description: 'OpenAI and Google credentials for chat and image-reading model calls.',
    secretIds: ['openai', 'google'],
  },
  {
    id: 'webSearch',
    label: 'Web search',
    description: 'Tavily credential for search-backed tools.',
    secretIds: ['tavily'],
  },
  {
    id: 'telegram',
    label: 'Telegram',
    description: 'Telegram bot credential and allowlist for the laptop connection bridge.',
    secretIds: ['telegramBot'],
  },
]

export const DEFAULT_CONNECTION_PORTAL: ConnectionPortalId = 'modelProviders'

export const CONNECTION_PORTAL_OPTIONS = CONNECTION_PORTALS.map((portal) => ({
  value: portal.id,
  label: portal.label,
}))
