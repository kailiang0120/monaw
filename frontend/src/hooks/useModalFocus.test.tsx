import { useRef, useState } from 'react'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { useModalFocus } from './useModalFocus'

function Dialog({ label, onEscape, children }: { label: string; onEscape: () => void; children?: React.ReactNode }) {
  const ref = useRef<HTMLDivElement>(null)
  useModalFocus(ref, { onEscape })
  return (
    <div ref={ref} role="dialog" aria-label={label} tabIndex={-1}>
      <button type="button">{label} first</button>
      <input aria-label={`${label} field`} />
      <button type="button" disabled>{label} disabled</button>
      <button type="button">{label} last</button>
      {children}
    </div>
  )
}

function Opener({ onEscape = () => {} }: { onEscape?: () => void }) {
  const [open, setOpen] = useState(false)
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>Open</button>
      {open && <Dialog label="Outer" onEscape={() => { onEscape(); setOpen(false) }} />}
    </>
  )
}

const tab = (shiftKey = false) => fireEvent.keyDown(document.activeElement ?? window, { key: 'Tab', shiftKey })

describe('useModalFocus', () => {
  afterEach(cleanup)

  it('focuses the first control and returns focus to the opener on close', () => {
    render(<Opener />)
    const opener = screen.getByRole('button', { name: 'Open' })
    opener.focus()

    fireEvent.click(opener)
    expect(screen.getByRole('button', { name: 'Outer first' })).toHaveFocus()

    fireEvent.keyDown(document.activeElement!, { key: 'Escape' })
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(opener).toHaveFocus()
  })

  it('keeps Tab and Shift+Tab inside the dialog, skipping disabled controls', () => {
    render(<Dialog label="Only" onEscape={() => {}} />)
    const first = screen.getByRole('button', { name: 'Only first' })
    const last = screen.getByRole('button', { name: 'Only last' })

    last.focus()
    tab()
    expect(first).toHaveFocus()

    tab(true)
    expect(last).toHaveFocus()
  })

  it('brings stray focus back into the dialog on Tab', () => {
    render(
      <>
        <button type="button">Outside</button>
        <Dialog label="Only" onEscape={() => {}} />
      </>,
    )
    screen.getByRole('button', { name: 'Outside' }).focus()

    tab()
    expect(screen.getByRole('button', { name: 'Only first' })).toHaveFocus()
  })

  it('leaves Escape to a control that already handled it', () => {
    const onEscape = vi.fn()
    render(
      <Dialog label="Only" onEscape={onEscape}>
        <input aria-label="Picker" onKeyDown={(event) => event.key === 'Escape' && event.preventDefault()} />
      </Dialog>,
    )

    fireEvent.keyDown(screen.getByLabelText('Picker'), { key: 'Escape' })
    expect(onEscape).not.toHaveBeenCalled()

    fireEvent.keyDown(screen.getByLabelText('Only field'), { key: 'Escape' })
    expect(onEscape).toHaveBeenCalledTimes(1)
  })

  it('lets only the innermost dialog handle keys', () => {
    const outerEscape = vi.fn()
    const innerEscape = vi.fn()
    render(
      <Dialog label="Outer" onEscape={outerEscape}>
        <Dialog label="Inner" onEscape={innerEscape} />
      </Dialog>,
    )

    expect(screen.getByRole('button', { name: 'Inner first' })).toHaveFocus()
    fireEvent.keyDown(document.activeElement!, { key: 'Escape' })

    expect(innerEscape).toHaveBeenCalledTimes(1)
    expect(outerEscape).not.toHaveBeenCalled()
  })
})
