import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { WorkspacePicker } from './WorkspacePicker'

afterEach(() => { cleanup(); delete window.electronAPI })

describe('conversation working folder', () => {
  it('opens the native picker and saves its selection', async () => {
    const selectDirectory = vi.fn().mockResolvedValue('C:\\Projects\\demo')
    Object.defineProperty(window, 'electronAPI', { configurable: true, writable: true, value: { selectDirectory } })
    const onChange = vi.fn().mockResolvedValue(undefined)
    render(<WorkspacePicker value="" onChange={onChange} />)
    expect(screen.getByText('Default workspace')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Choose working folder' }))
    await waitFor(() => expect(onChange).toHaveBeenCalledWith('C:\\Projects\\demo'))
    expect(selectDirectory).toHaveBeenCalledWith(undefined)
  })

  it('leaves the selected folder alone when the native dialog is cancelled', async () => {
    Object.defineProperty(window, 'electronAPI', { configurable: true, writable: true, value: { selectDirectory: vi.fn().mockResolvedValue('') } })
    const onChange = vi.fn()
    render(<WorkspacePicker value={'C:\\Projects\\demo'} onChange={onChange} />)
    fireEvent.click(screen.getByRole('button', { name: 'Choose working folder' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Choose working folder' })).toBeEnabled())
    expect(onChange).not.toHaveBeenCalled()
    expect(screen.getByText('demo')).toBeInTheDocument()
  })

  it('restores folders supplied by the conversation and resets to default', async () => {
    const onChange = vi.fn().mockResolvedValue(undefined)
    const { rerender } = render(<WorkspacePicker value={'C:\\Projects\\one'} onChange={onChange} />)
    expect(screen.getByRole('button', { name: 'Choose working folder' })).toHaveAttribute('title', 'C:\\Projects\\one')
    rerender(<WorkspacePicker value={'C:\\Projects\\two'} onChange={onChange} />)
    expect(screen.getByText('two')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Use default workspace' }))
    await waitFor(() => expect(onChange).toHaveBeenCalledWith(''))
  })

  it('supports entering a path in the browser and displays save failures', async () => {
    const onChange = vi.fn().mockRejectedValue(new Error('The working folder does not exist'))
    render(<WorkspacePicker value="" onChange={onChange} />)
    fireEvent.click(screen.getByRole('button', { name: 'Choose working folder' }))
    fireEvent.change(screen.getByRole('textbox', { name: 'Working folder path' }), { target: { value: 'C:\\missing' } })
    fireEvent.click(screen.getByRole('button', { name: 'Use folder' }))
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('The working folder does not exist'))
    expect(screen.getByRole('textbox', { name: 'Working folder path' })).toHaveValue('C:\\missing')
  })

  it('disables folder changes while a task is running', () => {
    render(<WorkspacePicker value={'C:\\Projects\\demo'} disabled onChange={vi.fn()} />)
    expect(screen.getByRole('button', { name: 'Choose working folder' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Use default workspace' })).toBeDisabled()
  })
})
