import { useState } from 'react'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { Dropdown } from './Dropdown'

const OPTIONS = [
  { value: 'low', label: 'Low' },
  { value: 'medium', label: 'Medium' },
  { value: 'high', label: 'High' },
] as const

type Level = (typeof OPTIONS)[number]['value']

function Harness({ onChange = () => {} }: { onChange?: (value: Level) => void }) {
  const [value, setValue] = useState<Level>('medium')
  return (
    <Dropdown<Level>
      ariaLabel="Level"
      value={value}
      options={[...OPTIONS]}
      onChange={(next) => {
        setValue(next)
        onChange(next)
      }}
    />
  )
}

const trigger = () => screen.getByRole('button', { name: 'Level' })
const option = (name: string) => screen.getByRole('option', { name })

describe('Dropdown keyboard use', () => {
  afterEach(cleanup)

  it('opens from the keyboard on the selected option', async () => {
    render(<Harness />)

    trigger().focus()
    fireEvent.keyDown(trigger(), { key: 'ArrowDown' })

    expect(trigger()).toHaveAttribute('aria-expanded', 'true')
    await waitFor(() => expect(option('Medium')).toHaveFocus())
  })

  it('moves between options with arrows, Home and End, wrapping at the ends', async () => {
    render(<Harness />)
    fireEvent.click(trigger())
    await waitFor(() => expect(option('Medium')).toHaveFocus())
    const listbox = screen.getByRole('listbox', { name: 'Level' })

    fireEvent.keyDown(listbox, { key: 'ArrowDown' })
    expect(option('High')).toHaveFocus()
    fireEvent.keyDown(listbox, { key: 'ArrowDown' })
    expect(option('Low')).toHaveFocus()
    fireEvent.keyDown(listbox, { key: 'ArrowUp' })
    expect(option('High')).toHaveFocus()
    fireEvent.keyDown(listbox, { key: 'Home' })
    expect(option('Low')).toHaveFocus()
    fireEvent.keyDown(listbox, { key: 'End' })
    expect(option('High')).toHaveFocus()
  })

  it('selects the focused option and returns focus to the trigger', async () => {
    const onChange = vi.fn()
    render(<Harness onChange={onChange} />)
    fireEvent.keyDown(trigger(), { key: 'ArrowDown' })
    await waitFor(() => expect(option('Medium')).toHaveFocus())
    fireEvent.keyDown(screen.getByRole('listbox'), { key: 'ArrowDown' })

    fireEvent.click(document.activeElement as HTMLElement)

    expect(onChange).toHaveBeenCalledWith('high')
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument()
    expect(trigger()).toHaveFocus()
    expect(trigger()).toHaveTextContent('High')
  })

  it('closes on Escape without letting the key reach an enclosing dialog', async () => {
    const dialogEscape = vi.fn()
    window.addEventListener('keydown', dialogEscape)
    try {
      render(<Harness />)
      fireEvent.click(trigger())
      await waitFor(() => expect(option('Medium')).toHaveFocus())

      fireEvent.keyDown(option('Medium'), { key: 'Escape' })

      expect(screen.queryByRole('listbox')).not.toBeInTheDocument()
      expect(trigger()).toHaveFocus()
      expect(dialogEscape).not.toHaveBeenCalled()
    } finally {
      window.removeEventListener('keydown', dialogEscape)
    }
  })

  it('closes when focus tabs out of the menu', async () => {
    render(<Harness />)
    fireEvent.click(trigger())
    await waitFor(() => expect(option('Medium')).toHaveFocus())

    fireEvent.keyDown(screen.getByRole('listbox'), { key: 'Tab' })

    expect(screen.queryByRole('listbox')).not.toBeInTheDocument()
    expect(trigger()).toHaveFocus()
  })
})
