import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { MCPServerConfig } from '../../lib/api/types'
import { McpCredentials } from './McpCredentials'

const server: MCPServerConfig = {
  server_id: 'a'.repeat(32), name: 'fixture', enabled: true, transport: 'stdio', command: 'fixture', args: [],
  env: { REGION: 'fixture-region' }, headers: {}, cwd: '', url: '', startup_timeout_ms: 8000,
  call_timeout_ms: 30000, reconnect_on_unhealthy: true, allow_list: [], trusted_tools: [], tool_risk_overrides: {}, description: '',
  credential_refs: { env: { API_KEY: 'mcp:v1:fixture-reference' } }, legacy_secret_fields: { env: ['LEGACY_TOKEN'] },
}

afterEach(() => { cleanup(); delete window.electronAPI })

function setup(disabled = false, change = vi.fn().mockResolvedValue({ ok: true })) {
  Object.defineProperty(window, 'electronAPI', { configurable: true, value: { changeMcpCredential: change } })
  const onChanged = vi.fn().mockResolvedValue(undefined)
  render(<McpCredentials server={server} disabled={disabled} onChanged={onChanged} />)
  return { change, onChanged }
}

describe('MCP credentials', () => {
  it('migrates a selected field without reading its value into the component', async () => {
    const { change, onChanged } = setup()
    fireEvent.click(screen.getByRole('button', { name: 'Encrypt LEGACY_TOKEN' }))
    await waitFor(() => expect(onChanged).toHaveBeenCalledOnce())
    expect(change).toHaveBeenCalledWith({ action: 'migrate', binding: { server_id: server.server_id, transport: 'stdio', target: 'env', key: 'LEGACY_TOKEN' } })
    expect(screen.getByLabelText('New credential value')).toHaveValue('')
    expect(screen.getByRole('status').textContent).toContain('OS encryption')
  })

  it('requires saved configuration and prevents repeated pending actions', async () => {
    const never = vi.fn(() => new Promise<{ ok: boolean }>(() => {}))
    setup(false, never)
    const button = screen.getByRole('button', { name: 'Encrypt LEGACY_TOKEN' })
    fireEvent.click(button)
    fireEvent.click(button)
    expect(never).toHaveBeenCalledOnce()
    expect(button).toBeDisabled()
  })

  it('disables immediate credential effects while configuration has unsaved changes', () => {
    const { change } = setup(true)
    fireEvent.click(screen.getByRole('button', { name: 'Encrypt LEGACY_TOKEN' }))
    expect(change).not.toHaveBeenCalled()
  })

  it('clears a successfully entered value and sends it only to scoped IPC', async () => {
    const { change } = setup()
    fireEvent.change(screen.getByLabelText('Credential environment variable'), { target: { value: 'API_KEY' } })
    fireEvent.change(screen.getByLabelText('New credential value'), { target: { value: 'fixture-private-value' } })
    expect(screen.getByLabelText('New credential value')).toHaveAttribute('type', 'password')
    fireEvent.click(screen.getByRole('button', { name: 'Store or replace credential' }))
    await waitFor(() => expect(screen.getByLabelText('New credential value')).toHaveValue(''))
    expect(change.mock.calls[0][0].value).toBe('fixture-private-value')
  })

  it('does not display raw exception text and preserves the entered value for explicit retry', async () => {
    const { onChanged } = setup(false, vi.fn().mockRejectedValue(new Error('echo fixture-private-value')))
    fireEvent.change(screen.getByLabelText('Credential environment variable'), { target: { value: 'API_KEY' } })
    fireEvent.change(screen.getByLabelText('New credential value'), { target: { value: 'fixture-private-value' } })
    fireEvent.click(screen.getByRole('button', { name: 'Store or replace credential' }))
    await waitFor(() => expect(screen.getByRole('status').textContent).toContain('retry to reconcile'))
    expect(screen.getByRole('status').textContent).not.toContain('fixture-private-value')
    expect(screen.getByLabelText('New credential value')).toHaveValue('fixture-private-value')
    expect(onChanged).not.toHaveBeenCalled()
  })
})
