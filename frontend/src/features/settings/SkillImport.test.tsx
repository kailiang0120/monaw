import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { SkillImport } from './SkillImport'
import { inspectSkills, installSkills } from '../../lib/api/skills'
import type { AgentSettings } from '../../lib/api/types'

vi.mock('../../lib/api/skills', () => ({ inspectSkills: vi.fn(), installSkills: vi.fn() }))
afterEach(() => { cleanup(); vi.clearAllMocks() })

it('previews a source and installs only selected skills', async () => {
  vi.mocked(inspectSkills).mockResolvedValue({ token: 'preview', skills: [
    { id: 'one/SKILL.md', name: 'one', description: 'First skill', files: 2, installed: false },
    { id: 'two/SKILL.md', name: 'two', description: 'Second skill', files: 1, installed: false },
    { id: 'core/SKILL.md', name: 'core', description: 'Existing skill', files: 1, installed: true },
  ] })
  const fresh = { tools: { skills: { one: true } } } as unknown as AgentSettings
  vi.mocked(installSkills).mockResolvedValue({ installed: ['one'], settings: fresh })
  const onInstalled = vi.fn()
  render(<SkillImport onInstalled={onInstalled} />)
  fireEvent.click(screen.getByRole('button', { name: 'Import' }))
  fireEvent.change(screen.getByLabelText('GitHub URL or local folder'), { target: { value: 'owner/repo' } })
  fireEvent.click(screen.getByRole('button', { name: 'Check source' }))
  await screen.findByRole('group', { name: 'Choose skills' })
  expect(screen.getByRole('checkbox', { name: /core/ })).toBeDisabled()
  fireEvent.click(screen.getByRole('checkbox', { name: /two/ }))
  fireEvent.click(screen.getByRole('button', { name: 'Install 1 skill' }))
  await waitFor(() => expect(onInstalled).toHaveBeenCalledWith(fresh, ['one']))
  expect(installSkills).toHaveBeenCalledWith('preview', ['one/SKILL.md'])
  expect(screen.getByRole('status')).toHaveTextContent('Ready for your next message')
})

it('clears stale previews and reports source failures', async () => {
  vi.mocked(inspectSkills).mockRejectedValue(new Error('No SKILL.md files found.'))
  render(<SkillImport onInstalled={vi.fn()} />)
  fireEvent.click(screen.getByRole('button', { name: 'Import' }))
  fireEvent.change(screen.getByLabelText('GitHub URL or local folder'), { target: { value: 'missing/repo' } })
  fireEvent.click(screen.getByRole('button', { name: 'Check source' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('No SKILL.md')
  fireEvent.change(screen.getByLabelText('GitHub URL or local folder'), { target: { value: 'other/repo' } })
  expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: /^Install/ })).not.toBeInTheDocument()
})
