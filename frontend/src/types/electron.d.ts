export {}

declare global {
  interface Window {
    electronAPI?: {
      storeGet: (key: string) => Promise<unknown>
      storeSet: (key: string, value: string) => Promise<void>
      storeDelete: (key: string) => Promise<void>
      selectDirectory?: (defaultPath?: string) => Promise<string>
      setTheme?: (theme: 'dark' | 'light') => Promise<void>
      backendBaseUrl: string
      isElectron: boolean
    }
  }
}
