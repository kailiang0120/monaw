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
      selectDirectory?: (defaultPath?: string) => Promise<string>
      setTheme?: (theme: 'dark' | 'light') => Promise<void>
      backendBaseUrl: string
      isElectron: boolean
    }
  }
}
