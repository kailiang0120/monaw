import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { ExecutionDetailSetting } from './ExecutionDetailSetting'

describe('ExecutionDetailSetting', () => {
  afterEach(cleanup)

  it('selects a presentation mode and states that it does not change permissions', () => {
    const onChange = vi.fn()
    render(<ExecutionDetailSetting value="simple" onChange={onChange} />)

    expect(screen.getByRole('button', { name: 'Concise' })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByText(/does not change permissions or how a Run executes/i)).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Developer' }))

    expect(onChange).toHaveBeenCalledWith('developer')
  })
})
