import { useState } from 'react'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { AgentSettings, MCPServerDiagnostics } from '../../lib/api/types'
import { McpSettings } from './McpSettings'
import { MCP_SERVER_TEMPLATES } from './settingsConfig'

const mocks = vi.hoisted(() => ({ reconnectMCPServer: vi.fn() }))

vi.mock('../../lib/api/diagnostics', () => ({ reconnectMCPServer: mocks.reconnectMCPServer }))

type Mcp = AgentSettings['mcp']

function Harness({
  initial,
  diagnostics = {},
  onMcp = () => {},
  onDiagnostic = () => {},
}: {
  initial: Mcp
  diagnostics?: Record<string, MCPServerDiagnostics>
  onMcp?: (mcp: Mcp) => void
  onDiagnostic?: (name: string, diagnostic: MCPServerDiagnostics) => void
}) {
  const [mcp, setMcp] = useState(initial)
  return (
    <McpSettings
      mcp={mcp}
      diagnostics={diagnostics}
      onChange={(updater) => setMcp((current) => {
        const next = updater(current)
        onMcp(next)
        return next
      })}
      onDiagnostic={onDiagnostic}
      credentialsDisabled={false}
      onCredentialsChanged={async () => {}}
    />
  )
}

function namedServer(name: string) {
  return { ...MCP_SERVER_TEMPLATES.custom.build(), name }
}

describe('McpSettings', () => {
  afterEach(() => {
    cleanup()
    mocks.reconnectMCPServer.mockReset()
  })

  it('adds, edits and removes servers in the MCP slice only', () => {
    const onMcp = vi.fn()
    render(<Harness initial={{ enabled: true, servers: [namedServer('notes')] }} onMcp={onMcp} />)

    fireEvent.click(screen.getByRole('button', { name: 'Add server' }))
    fireEvent.change(screen.getByRole('textbox', { name: 'Server name 2' }), { target: { value: 'notes' } })
    expect(screen.getByText('MCP server names must be unique. Duplicated: notes')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Remove MCP server 1' }))

    const latest = onMcp.mock.calls[onMcp.mock.calls.length - 1][0] as Mcp
    expect(latest.enabled).toBe(true)
    expect(latest.servers.map((server) => server.name)).toEqual(['notes'])
    expect(screen.queryByText(/must be unique/)).not.toBeInTheDocument()
  })

  it('reports a reconnected server diagnostic by name', async () => {
    const onDiagnostic = vi.fn()
    const diagnostic = { name: 'notes', connected: true, tool_count: 3, state: 'connected' } as MCPServerDiagnostics
    mocks.reconnectMCPServer.mockResolvedValue(diagnostic)
    render(<Harness initial={{ enabled: true, servers: [namedServer('notes')] }} onDiagnostic={onDiagnostic} />)

    fireEvent.click(screen.getByRole('button', { name: 'Reconnect' }))

    await waitFor(() => expect(onDiagnostic).toHaveBeenCalledWith('notes', diagnostic))
    expect(mocks.reconnectMCPServer).toHaveBeenCalledWith('notes')
    expect(screen.getByRole('button', { name: 'Reconnect' })).toBeEnabled()
  })
})
