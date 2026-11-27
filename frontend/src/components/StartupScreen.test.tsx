import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { StartupScreen } from './StartupScreen'

afterEach(() => {
  cleanup()
  vi.useRealTimers()
})

describe('StartupScreen', () => {
  it('renders determinate progress and clamps values between 0 and 100', () => {
    const { rerender } = render(
      <StartupScreen status="Starting Monaw…" progress={40} onRetry={() => {}} onContinue={() => {}} />,
    )

    const bar = screen.getByRole('progressbar', { name: 'Loading Monaw' })
    expect(bar).toHaveAttribute('aria-valuemin', '0')
    expect(bar).toHaveAttribute('aria-valuemax', '100')
    expect(bar).toHaveAttribute('aria-valuenow', '40')
    expect(bar.firstElementChild).toHaveStyle({ width: '40%' })
    expect(screen.getByRole('status')).toHaveTextContent('Starting Monaw…')
    expect(screen.getByText('40%')).toBeInTheDocument()

    rerender(
      <StartupScreen status="Loading preferences…" progress={140} onRetry={() => {}} onContinue={() => {}} />,
    )
    expect(bar).toHaveAttribute('aria-valuenow', '100')
    expect(bar.firstElementChild).toHaveStyle({ width: '100%' })
    expect(screen.getByText('100%')).toBeInTheDocument()

    rerender(
      <StartupScreen status="Starting Monaw…" progress={-10} onRetry={() => {}} onContinue={() => {}} />,
    )
    expect(bar).toHaveAttribute('aria-valuenow', '0')
    expect(bar.firstElementChild).toHaveStyle({ width: '0%' })
    expect(screen.getByText('0%')).toBeInTheDocument()
  })

  it('reveals slow-startup actions after 20 seconds', () => {
    vi.useFakeTimers()
    const onRetry = vi.fn()
    const onContinue = vi.fn()
    render(<StartupScreen status="Loading chats…" progress={65} onRetry={onRetry} onContinue={onContinue} />)

    expect(screen.queryByRole('button', { name: 'Try again' })).not.toBeInTheDocument()

    act(() => {
      vi.advanceTimersByTime(20_000)
    })

    expect(screen.getByRole('status')).toHaveTextContent('Startup is taking longer than expected.')
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }))
    fireEvent.click(screen.getByRole('button', { name: 'Open app' }))
    expect(onRetry).toHaveBeenCalledTimes(1)
    expect(onContinue).toHaveBeenCalledTimes(1)
  })
})
