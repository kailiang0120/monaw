import { updateSettings } from './api/settings'

type StoredApiKeys = {
  openaiKey: string
  deepseekKey: string
  googleKey: string
  tavilyKey: string
  telegramBotToken: string
  telegramAllowedUserIds: string
  telegramAllowedChatIds: string
}

async function readStoredApiKeys(): Promise<StoredApiKeys> {
  if (!window.electronAPI) {
    return {
      openaiKey: '',
      deepseekKey: '',
      googleKey: '',
      tavilyKey: '',
      telegramBotToken: '',
      telegramAllowedUserIds: '',
      telegramAllowedChatIds: '',
    }
  }
  const [
    openaiKey,
    deepseekKey,
    googleKey,
    tavilyKey,
    telegramBotToken,
    telegramAllowedUserIds,
    telegramAllowedChatIds,
  ] = await Promise.all([
    window.electronAPI.storeGet('openai_api_key'),
    window.electronAPI.storeGet('deepseek_api_key'),
    window.electronAPI.storeGet('google_api_key'),
    window.electronAPI.storeGet('tavily_api_key'),
    window.electronAPI.storeGet('telegram_bot_token'),
    window.electronAPI.storeGet('telegram_allowed_user_ids'),
    window.electronAPI.storeGet('telegram_allowed_chat_ids'),
  ])
  return {
    openaiKey: typeof openaiKey === 'string' ? openaiKey : '',
    deepseekKey: typeof deepseekKey === 'string' ? deepseekKey : '',
    googleKey: typeof googleKey === 'string' ? googleKey : '',
    tavilyKey: typeof tavilyKey === 'string' ? tavilyKey : '',
    telegramBotToken: typeof telegramBotToken === 'string' ? telegramBotToken : '',
    telegramAllowedUserIds: typeof telegramAllowedUserIds === 'string' ? telegramAllowedUserIds : '',
    telegramAllowedChatIds: typeof telegramAllowedChatIds === 'string' ? telegramAllowedChatIds : '',
  }
}

export async function syncStoredApiKeysToBackend(): Promise<StoredApiKeys> {
  const keys = await readStoredApiKeys()
  const payload: {
    openai_api_key?: string
    deepseek_api_key?: string
    google_api_key?: string
    tavily_api_key?: string
    telegram_bot_token?: string
    telegram_allowed_user_ids?: string
    telegram_allowed_chat_ids?: string
  } = {}
  if (keys.openaiKey) payload.openai_api_key = keys.openaiKey
  if (keys.deepseekKey) payload.deepseek_api_key = keys.deepseekKey
  if (keys.googleKey) payload.google_api_key = keys.googleKey
  if (keys.tavilyKey) payload.tavily_api_key = keys.tavilyKey
  if (keys.telegramBotToken) payload.telegram_bot_token = keys.telegramBotToken
  if (keys.telegramAllowedUserIds) payload.telegram_allowed_user_ids = keys.telegramAllowedUserIds
  if (keys.telegramAllowedChatIds) payload.telegram_allowed_chat_ids = keys.telegramAllowedChatIds
  if (Object.keys(payload).length > 0) {
    await updateSettings(payload)
  }
  return keys
}

export async function syncStoredApiKeysToBackendWithRetry(attempts = 5): Promise<void> {
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    try {
      await syncStoredApiKeysToBackend()
      return
    } catch {
      await new Promise((resolve) => window.setTimeout(resolve, 500 * (attempt + 1)))
    }
  }
}
