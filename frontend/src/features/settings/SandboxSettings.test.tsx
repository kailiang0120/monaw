import { useState } from 'react'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { AgentSettings, SandboxStatus } from '../../lib/api/types'
import { SandboxSettings } from './SandboxSettings'
import { emptySandbox } from './settingsConfig'
import { SANDBOX_COPY } from './settingsCopy'

const mocks = vi.hoisted(() => ({ resolveSandboxDockerImage: vi.fn() }))

vi.mock('../../lib/api/settings', () => ({ resolveSandboxDockerImage: mocks.resolveSandboxDockerImage }))

type Sandbox = AgentSettings['sandbox']

function Harness({
  initial,
  status = null,
  onSandbox,
  onStatus = () => {},
}: {
  initial: Sandbox
  status?: SandboxStatus | null
  onSandbox: (sandbox: Sandbox) => void
  onStatus?: (status: SandboxStatus) => void
}) {
  const [sandbox, setSandbox] = useState(initial)
  return (
    <SandboxSettings
      sandbox={sandbox}
      status={status}
      onChange={(updater) => setSandbox((current) => {
        const next = updater(current)
        onSandbox(next)
        return next
      })}
      onStatus={onStatus}
    />
  )
}

describe('SandboxSettings', () => {
  afterEach(() => {
    cleanup()
    mocks.resolveSandboxDockerImage.mockReset()
  })

  it('edits only the sandbox slice it is given', () => {
    const onSandbox = vi.fn()
    const initial = { ...emptySandbox(), enabled: true }
    render(<Harness initial={initial} onSandbox={onSandbox} />)

    fireEvent.click(screen.getByRole('checkbox', { name: 'Allow shell commands' }))
    fireEvent.change(screen.getByLabelText('Sandbox memory limit'), { target: { value: '2048' } })

    const latest = onSandbox.mock.calls[onSandbox.mock.calls.length - 1][0] as Sandbox
    expect(latest.enabled).toBe(false)
    expect(latest.resources.memory_mb).toBe(2048)
    expect(latest.docker).toEqual(initial.docker)
  })

  it('warns when strong isolation is required but Docker is unavailable', () => {
    const status = { backends: { docker: { available: false } } } as unknown as SandboxStatus
    render(<Harness initial={{ ...emptySandbox(), mode: 'enforce' }} status={status} onSandbox={() => {}} />)

    expect(screen.getByText(SANDBOX_COPY.enforceNeedsDocker)).toBeTruthy()
  })

  it('pins the resolved Docker image into the draft and reports the new status', async () => {
    const onSandbox = vi.fn()
    const onStatus = vi.fn()
    const resolvedStatus = { selected_backend: 'docker', isolation: 'strong' } as SandboxStatus
    mocks.resolveSandboxDockerImage.mockResolvedValue({
      image: 'python:3.12-slim@sha256:abc',
      detail: 'Pulled and pinned.',
      sandbox: resolvedStatus,
    })
    render(<Harness initial={{ ...emptySandbox(), docker: { ...emptySandbox().docker, image: 'python:3.12-slim' } }} onSandbox={onSandbox} onStatus={onStatus} />)

    fireEvent.click(screen.getByRole('button', { name: 'Resolve & pull' }))

    expect(await screen.findByText('Pulled and pinned.')).toBeTruthy()
    expect(mocks.resolveSandboxDockerImage).toHaveBeenCalledWith('python:3.12-slim')
    expect((screen.getByLabelText('Container image') as HTMLInputElement).value).toBe('python:3.12-slim@sha256:abc')
    expect(onStatus).toHaveBeenCalledWith(resolvedStatus)
  })

  it('keeps the typed image when resolving fails', async () => {
    const onSandbox = vi.fn()
    mocks.resolveSandboxDockerImage.mockRejectedValue(new Error('Docker is not running.'))
    render(<Harness initial={{ ...emptySandbox(), docker: { ...emptySandbox().docker, image: 'python:3.12-slim' } }} onSandbox={onSandbox} />)

    fireEvent.click(screen.getByRole('button', { name: 'Resolve & pull' }))

    expect(await screen.findByText('Docker is not running.')).toBeTruthy()
    expect(onSandbox).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: 'Resolve & pull' })).not.toHaveProperty('disabled', true)
  })
})
