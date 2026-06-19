export type StoredCredentialStatus = {
  openai: boolean
  deepseek: boolean
  google: boolean
  tavily: boolean
  telegramBot: boolean
}

const EMPTY_STATUS: StoredCredentialStatus = {
  openai: false,
  deepseek: false,
  google: false,
  tavily: false,
  telegramBot: false,
}

export async function syncStoredApiKeysToBackend(): Promise<StoredCredentialStatus> {
  if (!window.electronAPI) return EMPTY_STATUS
  return window.electronAPI.applyStoredCredentials()
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
