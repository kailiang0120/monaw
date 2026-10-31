export {}

type CredentialId = 'openai' | 'google' | 'tavily' | 'telegramBot'
type CredentialStatus = Record<CredentialId, boolean>

declare global {
  interface Window {
    electronAPI?: {
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
      backendBaseUrl: string
      platform?: string
      isElectron: boolean
    }
  }
}
