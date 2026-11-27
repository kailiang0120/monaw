import { apiFetch, BASE, JSON_HEADERS, throwApiError } from './client'

export interface ConnectorProvider {
  id: string
  name: string
  category?: string
  description?: string
  connection_method?: 'native' | 'composio'
  setup_notice?: string
  capabilities: Array<{ id: string; action: string; description: string }>
}

export interface ConnectorAccount {
  id: string
  provider_id: string
  label: string
  state: string
  enabled: boolean
  revision: number
  enabled_capabilities: string[]
}

export interface ConnectorLogin {
  login_id: string
  user_code: string
  interval: number
  expires_in: number
  verification_uri: string
}

/** Call the authenticated management API without handling provider credentials. */
export async function connectorRequest<T>(path: string, method = 'GET', body?: unknown): Promise<T> {
  const response = await apiFetch(`${BASE}/api/connectors${path}`, {
    method,
    headers: JSON_HEADERS,
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  if (!response.ok) await throwApiError(response, 'Could not update connectors. Try again.')
  return response.json() as Promise<T>
}
