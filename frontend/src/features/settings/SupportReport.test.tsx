import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { SupportReport } from './SupportReport'

const mocks = vi.hoisted(() => ({ fetchSupportReport: vi.fn() }))

vi.mock('../../lib/api/diagnostics', () => ({ fetchSupportReport: mocks.fetchSupportReport }))

afterEach(() => {
  cleanup()
  mocks.fetchSupportReport.mockReset()
})

describe('SupportReport', () => {
  it('shows the whole report before it can be saved', async () => {
    mocks.fetchSupportReport.mockResolvedValue({
      format: 'monaw-support-report/1',
      backend_log: { included: false, lines: [], note: 'Turn on support mode to include the backend log.' },
      recent_errors: [{ message: 'reading [REDACTED PATH]' }],
      truncated: false,
    })
    render(<SupportReport />)
    expect(screen.queryByRole('button', { name: /Save report/ })).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Prepare report' }))

    const contents = await screen.findByLabelText('Support report contents')
    expect(contents).toHaveTextContent('monaw-support-report/1')
    expect(contents).toHaveTextContent('[REDACTED PATH]')
    expect(screen.getByText(/backend log is left out/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Save report/ })).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Discard' }))
    expect(screen.queryByLabelText('Support report contents')).not.toBeInTheDocument()
  })

  it('reports a failure to prepare it', async () => {
    mocks.fetchSupportReport.mockRejectedValue(new Error('Failed to prepare the support report'))
    render(<SupportReport />)

    fireEvent.click(screen.getByRole('button', { name: 'Prepare report' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('Failed to prepare the support report')
  })
})
