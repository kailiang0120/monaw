export {}

type CredentialId = 'openai' | 'google' | 'tavily' | 'telegramBot'
type CredentialStatus = Record<CredentialId, boolean>

declare global {
  type UpdateState = {
    currentVersion: string
    status: 'unavailable' | 'idle' | 'checking' | 'up-to-date' | 'downloading' | 'ready' | 'error'
    latestVersion?: string
    progress?: number
    error?: string
  }
  interface Window {
    electronAPI?: {
      updateStatus?: () => Promise<UpdateState>
      checkForUpdates?: () => Promise<UpdateState>
      installUpdate?: () => Promise<boolean>
      onUpdateState?: (listener: (state: UpdateState) => void) => () => void
      getControlSession: () => Promise<{ token: string; expiresAt: number }>
      credentialStatus: () => Promise<CredentialStatus>
      setCredential: (id: CredentialId, value: string) => Promise<CredentialStatus>
      deleteCredential: (id: CredentialId) => Promise<CredentialStatus>
      applyStoredCredentials: () => Promise<CredentialStatus>
      changeMcpCredential: (input: {
        action: 'migrate' | 'replace' | 'delete'
        binding: { server_id: string; transport: 'stdio' | 'streamable_http'; target: 'env' | 'headers'; key: string }
        value?: string
      }) => Promise<{ ok: boolean }>
      selectDirectory?: (defaultPath?: string) => Promise<string>
      setTheme?: (theme: 'dark' | 'light') => Promise<void>
      writeClipboardText?: (text: string) => Promise<void>
      windowControl?: (action: 'minimize' | 'toggle-maximize' | 'close') => Promise<boolean>
      isWindowMaximized?: () => Promise<boolean>
      /** Returns a function that stops listening. */
      onWindowMaximized?: (listener: (maximized: boolean) => void) => () => void
      /** Clears the window's HTTP, code and shader caches. */
      clearAppCache?: () => Promise<boolean>
      backendBaseUrl: string
      platform?: string
      isElectron: boolean
    }
  }
}
